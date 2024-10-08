# coding=utf-8
# Copyright 2023 Mistral AI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
""" MindSpore Mixtral model."""
import math
from typing import List, Optional, Tuple, Union

import numpy as np
import mindspore
from mindspore import Tensor, Parameter
from mindspore.common.initializer import initializer, Normal

from mindnlp.core import nn, ops, get_default_dtype
from mindnlp.core.nn import functional as F
from mindnlp.core.nn import CrossEntropyLoss
from mindnlp.utils import logging
from ...activations import ACT2FN
from ...cache_utils import Cache, DynamicCache
from ...modeling_attn_mask_utils import (
    _prepare_4d_causal_attention_mask,
)
from ...modeling_outputs import (
    MoeCausalLMOutputWithPast,
    MoeModelOutputWithPast,
    SequenceClassifierOutputWithPast,
    TokenClassifierOutput,
)
from ...modeling_utils import PreTrainedModel
from .configuration_mixtral import MixtralConfig

logger = logging.get_logger(__name__)

_CONFIG_FOR_DOC = "MixtralConfig"


def load_balancing_loss_func(
    gate_logits: mindspore.Tensor, num_experts: mindspore.Tensor = None, top_k=2, attention_mask: Optional[mindspore.Tensor] = None
) -> float:
    r"""
    Computes auxiliary load balancing loss as in Switch Transformer - implemented in MindSpore.

    See Switch Transformer (https://arxiv.org/abs/2101.03961) for more details. This function implements the loss
    function presented in equations (4) - (6) of the paper. It aims at penalizing cases where the routing between
    experts is too unbalanced.

    Args:
        gate_logits (Union[`mindspore.Tensor`, Tuple[mindspore.Tensor]):
            Logits from the `gate`, should be a tuple of model.config.num_hidden_layers tensors of
            shape [batch_size X sequence_length, num_experts].
        attention_mask (`mindspore.Tensor`, None):
            The attention_mask used in forward function
            shape [batch_size X sequence_length] if not None.
        num_experts (`int`, *optional*):
            Number of experts

    Returns:
        The auxiliary loss.
    """
    if gate_logits is None or not isinstance(gate_logits, tuple):
        return 0

    if isinstance(gate_logits, tuple):
        concatenated_gate_logits = ops.cat(list(gate_logits), dim=0)

    routing_weights = ops.softmax(concatenated_gate_logits, dim=-1)

    _, selected_experts = ops.topk(routing_weights, top_k, dim=-1)

    expert_mask = F.one_hot(selected_experts, num_experts)

    if attention_mask is None:
        # Compute the percentage of tokens routed to each experts
        tokens_per_expert = ops.mean(expert_mask.float(), dim=0)

        # Compute the average probability of routing to these experts
        router_prob_per_expert = ops.mean(routing_weights, dim=0)
    else:
        batch_size, sequence_length = attention_mask.shape
        num_hidden_layers = concatenated_gate_logits.shape[0] // (batch_size * sequence_length)

        # Compute the mask that masks all padding tokens as 0 with the same shape of expert_mask
        expert_attention_mask = (
            attention_mask[None, :, :, None, None]
            .broadcast_to((num_hidden_layers, batch_size, sequence_length, top_k, num_experts))
            .reshape(-1, top_k, num_experts)
        )

        # Compute the percentage of tokens routed to each experts
        tokens_per_expert = ops.sum(expert_mask.float() * expert_attention_mask, dim=0) / ops.sum(
            expert_attention_mask, dim=0
        )

        # Compute the mask that masks all padding tokens as 0 with the same shape of tokens_per_expert
        router_per_expert_attention_mask = (
            attention_mask[None, :, :, None]
            .broadcast_to((num_hidden_layers, batch_size, sequence_length, num_experts))
            .reshape(-1, num_experts)
        )

        # Compute the average probability of routing to these experts
        router_prob_per_expert = ops.sum(routing_weights * router_per_expert_attention_mask, dim=0) / ops.sum(
            router_per_expert_attention_mask, dim=0
        )

    overall_loss = ops.sum(tokens_per_expert * router_prob_per_expert.unsqueeze(0))
    return overall_loss * num_experts


# Copied from transformers.models.llama.modeling_llama._get_unpad_data
def _get_unpad_data(attention_mask):
    '''
    This function retrieves unpad data from the attention mask.
    
    Args:
        attention_mask (Tensor): A tensor representing the attention mask for the input data.
    
    Returns:
        tuple:
            A tuple containing the following:

            - indices (Tensor): A tensor containing the indices of the flattened attention mask.
            - cu_seqlens (Tensor): A tensor representing the cumulative sequence lengths based on the attention mask.
            - max_seqlen_in_batch (int): The maximum sequence length in the batch.

    Raises:
        None
    '''
    seqlens_in_batch = attention_mask.sum(dim=-1, dtype=mindspore.int32)
    indices = ops.nonzero(attention_mask.flatten()).flatten()
    max_seqlen_in_batch = seqlens_in_batch.max().item()
    cu_seqlens = ops.pad(ops.cumsum(seqlens_in_batch, dim=0, dtype=mindspore.int32), (1, 0))
    return (
        indices,
        cu_seqlens,
        max_seqlen_in_batch,
    )


# Copied from transformers.models.llama.modeling_llama.LlamaRMSNorm with Llama->Mixtral
class MixtralRMSNorm(nn.Module):

    """
    The MixtralRMSNorm class is a custom implementation of the T5LayerNorm, which is used for normalization in
    neural networks.

    This class inherits from the nn.Module class and provides methods to perform RMS normalization on hidden states.

    Attributes:
        weight (Parameter): A learnable parameter that scales the normalized hidden states.
        variance_epsilon (float): A small epsilon value added to the variance to avoid division by zero.

    Methods:
        __init__: Initializes the MixtralRMSNorm instance with the given hidden size and epsilon value.
        forward: Applies the RMS normalization on the input hidden states and returns the normalized result.

    Note:
        - MixtralRMSNorm is equivalent to T5LayerNorm.

    """
    def __init__(self, hidden_size, eps=1e-6):
        """
        MixtralRMSNorm is equivalent to T5LayerNorm
        """
        super().__init__()
        self.weight = Parameter(ops.ones(hidden_size))
        self.variance_epsilon = eps

    def forward(self, hidden_states):
        """
        This method 'forward' is defined within the 'MixtralRMSNorm' class and is used to perform a specific
        computation on the input hidden states.

        Args:
            self: Represents the instance of the class. It is automatically passed when the method is called.
                No specific restrictions apply.
            hidden_states: Represents the input hidden states tensor. It should be of type mindspore.Tensor or compatible.
                No specific restrictions apply.

        Returns:
            None: This method does not return any value. It performs in-place operations on the input hidden_states.

        Raises:
            NotImplementedError: If the method or a specific operation within the method is not implemented.
            ValueError: If the input hidden_states is not of the expected data type or format.
            RuntimeError: If an error occurs during the computation process.
        """
        input_dtype = hidden_states.dtype
        hidden_states = hidden_states.to(mindspore.float32)
        variance = hidden_states.pow(2).mean(-1, keep_dims=True)
        hidden_states = hidden_states * ops.rsqrt(variance + self.variance_epsilon)
        return self.weight * hidden_states.to(input_dtype)


# Copied from transformers.models.mistral.modeling_mistral.MistralRotaryEmbedding with Mistral->Mixtral
class MixtralRotaryEmbedding(nn.Module):

    """
    A class representing MixtralRotaryEmbedding, a neural network module used for Rotary Positional Embedding in
    Mixtral models.

    This class inherits from nn.Module and provides methods to initialize the embedding, set the cosine and sine cache,
    and forward the embedding for a given input sequence.

    Attributes:
        dim (int): The dimension of the embedding.
        max_position_embeddings (int): The maximum number of position embeddings.
        base (int): The base value used in the inverse frequency calculation.
        inv_freq (Tensor): The inverse frequency tensor used for the embedding.
        max_seq_len_cached (int): The maximum sequence length up to which the cosine and sine cache is calculated.
        cos_cached (Tensor): The cosine cache tensor.
        sin_cached (Tensor): The sine cache tensor.

    Methods:
        __init__:
            Initializes a MixtralRotaryEmbedding instance with the specified dimension, maximum position embeddings,
            and base value.

        _set_cos_sin_cache:
            Sets the cosine and sine cache for the specified sequence length and data type.

        forward:
            Constructs the rotary positional embedding for the given input sequence.

    Note:
        This class is designed for use in Mixtral models and is intended to be used as a part of a
        larger neural network architecture.
    """
    def __init__(self, dim, max_position_embeddings=2048, base=10000):
        """
        __init__(self, dim, max_position_embeddings=2048, base=10000)

        Initialize the MixtralRotaryEmbedding instance with the specified parameters.

        Args:
            self: The instance of the MixtralRotaryEmbedding class.
            dim (int): The dimension of the embedding.
            max_position_embeddings (int, optional): The maximum number of position embeddings. Defaults to 2048.
            base (int, optional): The base value for computing the inverse frequency. Defaults to 10000.

        Returns:
            None.

        Raises:
            TypeError: If the provided 'dim', 'max_position_embeddings', or 'base' is not of type int.
            ValueError: If 'dim' is not a positive integer or 'max_position_embeddings' or 'base' is not a
                non-negative integer.
        """
        super().__init__()

        self.dim = dim
        self.max_position_embeddings = max_position_embeddings
        self.base = base
        inv_freq = 1.0 / (self.base ** (ops.arange(0, self.dim, 2, dtype=mindspore.int64).float() / self.dim))
        self.inv_freq = inv_freq

        self._set_cos_sin_cache(
            seq_len=max_position_embeddings, dtype=get_default_dtype()
        )

    def _set_cos_sin_cache(self, seq_len, dtype):
        """Set cosine and sine cache for MixtralRotaryEmbedding.

        This method calculates and stores the cosine and sine values for the MixtralRotaryEmbedding class.
        These values are used in the embedding calculations for the given sequence length and data type.

        Args:
            self (MixtralRotaryEmbedding): The instance of the MixtralRotaryEmbedding class.
            seq_len (int): The length of the sequence for which the cosine and sine values are calculated.
            dtype (type): The data type of the cosine and sine values.

        Returns:
            None.

        Raises:
            None.
        """
        self.max_seq_len_cached = seq_len
        t = ops.arange(self.max_seq_len_cached, dtype=mindspore.int64).astype(self.inv_freq.dtype)

        freqs = ops.outer(t, self.inv_freq)
        # Different from paper, but it uses a different permutation in order to obtain the same calculation
        emb = ops.cat((freqs, freqs), dim=-1)
        self.cos_cached = emb.cos().to(dtype)
        self.sin_cached = emb.sin().to(dtype)

    def forward(self, x, seq_len=None):
        """
        This method forwards a Mixtral Rotary Embedding based on the input parameters.

        Args:
            self: The instance of the MixtralRotaryEmbedding class.
            x: The input tensor for which the embedding is forwarded.
            seq_len: An integer representing the sequence length of the embedding. Default is None.

        Returns:
            None.

        Raises:
            ValueError: If seq_len is greater than the maximum sequence length cached in the object.
        """
        # x: [bs, num_attention_heads, seq_len, head_size]
        if seq_len > self.max_seq_len_cached:
            self._set_cos_sin_cache(seq_len=seq_len, dtype=x.dtype)

        return (
            self.cos_cached[:seq_len].to(dtype=x.dtype),
            self.sin_cached[:seq_len].to(dtype=x.dtype),
        )


# Copied from transformers.models.llama.modeling_llama.rotate_half
def rotate_half(x):
    """Rotates half the hidden dims of the input."""
    # x1 = x[..., : x.shape[-1] // 2]
    # x2 = x[..., x.shape[-1] // 2 :]
    x1, x2 = x.tensor_split(2, -1)
    return ops.cat((-x2, x1), dim=-1)


# Copied from transformers.models.mistral.modeling_mistral.apply_rotary_pos_emb
def apply_rotary_pos_emb(q, k, cos, sin, position_ids, unsqueeze_dim=1):
    """Applies Rotary Position Embedding to the query and key tensors.

    Args:
        q (`mindspore.Tensor`): The query tensor.
        k (`mindspore.Tensor`): The key tensor.
        cos (`mindspore.Tensor`): The cosine part of the rotary embedding.
        sin (`mindspore.Tensor`): The sine part of the rotary embedding.
        position_ids (`mindspore.Tensor`):
            The position indices of the tokens corresponding to the query and key tensors. For example, this can be
            used to pass offsetted position ids when working with a KV-cache.
        unsqueeze_dim (`int`, *optional*, defaults to 1):
            The 'unsqueeze_dim' argument specifies the dimension along which to unsqueeze cos[position_ids] and
            sin[position_ids] so that they can be properly broadcasted to the dimensions of q and k. For example, note
            that cos[position_ids] and sin[position_ids] have the shape [batch_size, seq_len, head_dim]. Then, if q and
            k have the shape [batch_size, heads, seq_len, head_dim], then setting unsqueeze_dim=1 makes
            cos[position_ids] and sin[position_ids] broadcastable to the shapes of q and k. Similarly, if q and k have
            the shape [batch_size, seq_len, heads, head_dim], then set unsqueeze_dim=2.
    Returns:
        `tuple(mindspore.Tensor)` comprising of the query and key tensors rotated using the Rotary Position Embedding.
    """
    cos = cos[position_ids].unsqueeze(unsqueeze_dim)
    sin = sin[position_ids].unsqueeze(unsqueeze_dim)
    q_embed = (q * cos) + (rotate_half(q) * sin)
    k_embed = (k * cos) + (rotate_half(k) * sin)
    return q_embed, k_embed


# Copied from transformers.models.llama.modeling_llama.repeat_kv
def repeat_kv(hidden_states: mindspore.Tensor, n_rep: int) -> mindspore.Tensor:
    """
    This is the equivalent of torch.repeat_interleave(x, dim=1, repeats=n_rep). The hidden states go from (batch,
    num_key_value_heads, seqlen, head_dim) to (batch, num_attention_heads, seqlen, head_dim)
    """
    batch, num_key_value_heads, slen, head_dim = hidden_states.shape
    if n_rep == 1:
        return hidden_states
    hidden_states = hidden_states[:, :, None, :, :].broadcast_to((batch, num_key_value_heads, n_rep, slen, head_dim))
    return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)


# Copied from transformers.models.mistral.modeling_mistral.MistralAttention with Mistral->Mixtral
class MixtralAttention(nn.Module):
    """
    Multi-headed attention from 'Attention Is All You Need' paper. Modified to use sliding window attention: Longformer
    and "Generating Long Sequences with Sparse Transformers".
    """
    def __init__(self, config: MixtralConfig, layer_idx: Optional[int] = None):
        """
        Initializes an instance of the MixtralAttention class.

        Args:
            self: The object instance.
            config (MixtralConfig): An instance of the MixtralConfig class containing the configuration parameters
                for the attention layer.
            layer_idx (Optional[int]): The index of the layer. Defaults to None. If layer_idx is not provided,
                a warning will be logged, as not passing a `layer_idx` is not recommended and may cause errors
                during the forward call if caching is used.

        Returns:
            None.

        Raises:
            ValueError: If the `hidden_size` is not divisible by `num_heads`.
        """
        super().__init__()
        self.config = config
        self.layer_idx = layer_idx
        if layer_idx is None:
            logger.warning_once(
                f"Instantiating {self.__class__.__name__} without passing a `layer_idx` is not recommended and will "
                "lead to errors during the forward call if caching is used. Please make sure to provide a `layer_idx` "
                "when creating this class."
            )

        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads
        self.num_key_value_heads = config.num_key_value_heads
        self.num_key_value_groups = self.num_heads // self.num_key_value_heads
        self.max_position_embeddings = config.max_position_embeddings
        self.rope_theta = config.rope_theta
        self.is_causal = True
        self.attention_dropout = config.attention_dropout

        if (self.head_dim * self.num_heads) != self.hidden_size:
            raise ValueError(
                f"hidden_size must be divisible by num_heads (got `hidden_size`: {self.hidden_size}"
                f" and `num_heads`: {self.num_heads})."
            )
        self.q_proj = nn.Linear(self.hidden_size, self.num_heads * self.head_dim, bias=False)
        self.k_proj = nn.Linear(self.hidden_size, self.num_key_value_heads * self.head_dim, bias=False)
        self.v_proj = nn.Linear(self.hidden_size, self.num_key_value_heads * self.head_dim, bias=False)
        self.o_proj = nn.Linear(self.num_heads * self.head_dim, self.hidden_size, bias=False)

        self.rotary_emb = MixtralRotaryEmbedding(
            self.head_dim,
            max_position_embeddings=self.max_position_embeddings,
            base=self.rope_theta,
        )

    def _shape(self, tensor: mindspore.Tensor, seq_len: int, bsz: int):
        """
        This method reshapes the input tensor for the MixtralAttention layer.

        Args:
            self (MixtralAttention): An instance of the MixtralAttention class.
            tensor (mindspore.Tensor): The input tensor to be reshaped.
            seq_len (int): The length of the sequence in the tensor.
            bsz (int): The batch size of the tensor.

        Returns:
            None

        Raises:
            None

        This method reshapes the input tensor by rearranging its dimensions. The tensor is reshaped into a new shape of
        (bsz, seq_len, num_heads, head_dim) by using the view and swapaxes operations. The returned tensor has its
        dimensions rearranged to facilitate further processing in the MixtralAttention layer.
        """
        return tensor.view(bsz, seq_len, self.num_heads, self.head_dim).swapaxes(1, 2)

    def forward(
        self,
        hidden_states: mindspore.Tensor,
        attention_mask: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        past_key_value: Optional[Cache] = None,
        output_attentions: bool = False,
        **kwargs,
    ) -> Tuple[mindspore.Tensor, Optional[mindspore.Tensor], Optional[Tuple[mindspore.Tensor]]]:
        '''
        Construct method in the MixtralAttention class.

        Args:
            self: The instance of the class.
            hidden_states (mindspore.Tensor): The input tensor of shape (batch_size, sequence_length, hidden_size).
            attention_mask (Optional[mindspore.Tensor]): An optional tensor of shape
                (batch_size, 1, sequence_length, sequence_length) containing the attention mask.
            position_ids (Optional[mindspore.Tensor]): An optional tensor containing the position indices of shape
                (batch_size, sequence_length).
            past_key_value (Optional[Cache]): An optional caching mechanism for previous key and value tensors.
            output_attentions (bool): A boolean flag indicating whether to return the attention weights.

        Returns:
            Tuple[mindspore.Tensor, Optional[mindspore.Tensor], Optional[Tuple[mindspore.Tensor]]]: A tuple containing
                the attention output tensor of shape (batch_size, sequence_length, hidden_size),
            optional attention weights tensor, and optional new key-value cache tuple.

        Raises:
            ValueError: If the cache structure has changed, attention weights or attention mask have invalid shapes,
                or if the attention output has an unexpected shape.
        '''
        bsz, q_len, _ = hidden_states.shape

        query_states = self.q_proj(hidden_states)
        key_states = self.k_proj(hidden_states)
        value_states = self.v_proj(hidden_states)

        query_states = query_states.view(bsz, q_len, self.num_heads, self.head_dim).swapaxes(1, 2)
        key_states = key_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).swapaxes(1, 2)
        value_states = value_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).swapaxes(1, 2)

        kv_seq_len = key_states.shape[-2]
        if past_key_value is not None:
            if self.layer_idx is None:
                raise ValueError(
                    f"The cache structure has changed since version v4.36. If you are using {self.__class__.__name__} "
                    "for auto-regressive decoding with k/v caching, please make sure to initialize the attention class "
                    "with a layer index."
                )
            kv_seq_len += past_key_value.get_usable_length(kv_seq_len, self.layer_idx)
        cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin, position_ids)

        if past_key_value is not None:
            cache_kwargs = {"sin": sin, "cos": cos}  # Specific to RoPE models
            key_states, value_states = past_key_value.update(key_states, value_states, self.layer_idx, cache_kwargs)

        # repeat k/v heads if n_kv_heads < n_heads
        key_states = repeat_kv(key_states, self.num_key_value_groups)
        value_states = repeat_kv(value_states, self.num_key_value_groups)

        attn_weights = ops.matmul(query_states, key_states.swapaxes(2, 3)) / math.sqrt(self.head_dim)

        if attn_weights.shape != (bsz, self.num_heads, q_len, kv_seq_len):
            raise ValueError(
                f"Attention weights should be of size {(bsz, self.num_heads, q_len, kv_seq_len)}, but is"
                f" {attn_weights.shape}"
            )

        if attention_mask is not None:
            if attention_mask.shape != (bsz, 1, q_len, kv_seq_len):
                raise ValueError(
                    f"Attention mask should be of size {(bsz, 1, q_len, kv_seq_len)}, but is {attention_mask.shape}"
                )

            attn_weights = attn_weights + attention_mask
        # upcast attention to fp32
        attn_weights = ops.softmax(attn_weights, dim=-1, dtype=mindspore.float32).to(query_states.dtype)
        attn_weights = F.dropout(attn_weights, p=self.attention_dropout, training=self.training)
        attn_output = ops.matmul(attn_weights, value_states)
        if attn_output.shape != (bsz, self.num_heads, q_len, self.head_dim):
            raise ValueError(
                f"`attn_output` should be of size {(bsz, self.num_heads, q_len, self.head_dim)}, but is"
                f" {attn_output.shape}"
            )

        attn_output = attn_output.swapaxes(1, 2)
        attn_output = attn_output.reshape(bsz, q_len, self.hidden_size)

        attn_output = self.o_proj(attn_output)

        if not output_attentions:
            attn_weights = None

        return attn_output, attn_weights, past_key_value


MIXTRAL_ATTENTION_CLASSES = {
    "eager": MixtralAttention,
}


class MixtralBlockSparseTop2MLP(nn.Module):

    """
    The MixtralBlockSparseTop2MLP class represents a neural network block that utilizes sparse top-2 multi-layer
    perceptron (MLP) for processing hidden states. It inherits from nn.Module and includes methods for initialization
    and forwardion of the MLP layers.

    Attributes:
        ffn_dim (int): The dimension of the feed-forward network.
        hidden_dim (int): The dimension of the hidden layer in the network.
        w1 (nn.Linear): The first dense layer in the MLP with hidden_dim input and ffn_dim output.
        w2 (nn.Linear): The second dense layer in the MLP with ffn_dim input and hidden_dim output.
        w3 (nn.Linear): The third dense layer in the MLP with hidden_dim input and ffn_dim output.
        act_fn (function): The activation function to be applied on the hidden states.

    Methods:
        __init__: Initializes the MixtralBlockSparseTop2MLP instance with the provided configuration.
        forward: Constructs the sparse top-2 MLP using the provided hidden states and returns the processed
            hidden states.

    Note:
        The code provided in the class is an example and may not fully represent the functionality of the
        MixtralBlockSparseTop2MLP class.
    """
    def __init__(self, config: MixtralConfig):
        '''
        Initializes a MixtralBlockSparseTop2MLP instance.

        Args:
            self: The instance itself.
            config (MixtralConfig): An instance of MixtralConfig containing the configuration settings
                for the MixtralBlockSparseTop2MLP.

        Returns:
            None.

        Raises:
            None.
        '''
        super().__init__()
        self.ffn_dim = config.intermediate_size
        self.hidden_dim = config.hidden_size

        self.w1 = nn.Linear(self.hidden_dim, self.ffn_dim, bias=False)
        self.w2 = nn.Linear(self.ffn_dim, self.hidden_dim, bias=False)
        self.w3 = nn.Linear(self.hidden_dim, self.ffn_dim, bias=False)

        self.act_fn = ACT2FN[config.hidden_act]

    def forward(self, hidden_states):
        """
        Constructs the current hidden states using the provided hidden states.

        Args:
            self (MixtralBlockSparseTop2MLP): The instance of the MixtralBlockSparseTop2MLP class.
            hidden_states (tensor): The input hidden states to be used for forwarding the current hidden states.

        Returns:
            tensor: The current hidden states forwarded based on the input hidden states.

        Raises:
            ValueError: If the input hidden_states is not in the expected format.
            RuntimeError: If there is an issue with the execution of the method.
        """
        current_hidden_states = self.act_fn(self.w1(hidden_states)) * self.w3(hidden_states)
        current_hidden_states = self.w2(current_hidden_states)
        return current_hidden_states


class MixtralSparseMoeBlock(nn.Module):
    """
    This implementation is strictly equivalent to standard MoE with full capacity (no dropped tokens).
    It's faster since it formulates MoE operations in terms of block-sparse operations to accomodate imbalanced
    assignments of tokens to experts, whereas standard MoE either (1) drop tokens at the cost of reduced performance
    or (2) set capacity factor to number of experts and thus waste computation and memory on padding.
    """
    def __init__(self, config):
        """
        Initializes an instance of the MixtralSparseMoeBlock class.

        Args:
            self: An instance of the MixtralSparseMoeBlock class.
            config:
                A configuration object containing the following attributes:

                - hidden_size (int): The dimension of the hidden layer.
                - intermediate_size (int): The dimension of the feed-forward network.
                - num_local_experts (int): The number of local experts.
                - num_experts_per_tok (int): The number of experts per token.

        Returns:
            None.

        Raises:
            TypeError: If the provided config parameter is not of the expected type.
            ValueError: If the hidden_size, intermediate_size, num_local_experts,
                or num_experts_per_tok attributes are missing in the config object.
            ValueError: If the hidden_size, intermediate_size, num_local_experts,
                or num_experts_per_tok attributes are not integers.
        """
        super().__init__()
        self.hidden_dim = config.hidden_size
        self.ffn_dim = config.intermediate_size
        self.num_experts = config.num_local_experts
        self.top_k = config.num_experts_per_tok

        # gating
        self.gate = nn.Linear(self.hidden_dim, self.num_experts, bias=False)

        self.experts = nn.ModuleList([MixtralBlockSparseTop2MLP(config) for _ in range(self.num_experts)])

    def forward(self, hidden_states: mindspore.Tensor) -> mindspore.Tensor:
        """
        Constructs the MixtralSparseMoeBlock.

        Args:
            self (MixtralSparseMoeBlock): The instance of the MixtralSparseMoeBlock class.
            hidden_states (mindspore.Tensor): The input hidden states tensor of shape
                (batch_size, sequence_length, hidden_dim).

        Returns:
            mindspore.Tensor: The final hidden states tensor after applying the MixtralSparseMoeBlock,
                of shape (batch_size, sequence_length, hidden_dim).

        Raises:
            None.

        This method forwards the MixtralSparseMoeBlock by applying the following steps:

        1. Reshapes the hidden_states tensor to (-1, hidden_dim).
        2. Computes the router logits by passing the reshaped hidden_states through the gate module.
        3. Computes the routing weights by applying softmax to the router logits along axis 1.
        4. Selects the top-k routing weights and corresponding indices.
        5. Normalizes the routing weights.
        6. Converts the routing weights to the same data type as hidden_states.
        7. Initializes the final_hidden_states tensor with zeros of shape (batch_size * sequence_length, hidden_dim).
        8. Generates the expert_mask tensor using one_hot encoding and permutation.
        9. Iterates over each expert and performs the following steps:

            - Retrieves the non-zero indices from the expert_mask for the current expert.
            - Splits the non-zero indices tensor into index and top_x tensors.
            - If top_x tensor is empty, continue to the next iteration.
            - Retrieves the current hidden states by indexing the hidden_states tensor with top_x.
            - Computes the current hidden states using the expert_layer and routing_weights.
            - Updates the final_hidden_states tensor by adding the computed current_hidden_states using index_add.

        10. Reshapes the final_hidden_states tensor to its original shape (batch_size, sequence_length, hidden_dim).
        11. Returns the final_hidden_states tensor and the router_logits tensor.
        """
        batch_size, sequence_length, hidden_dim = hidden_states.shape
        hidden_states = hidden_states.view(-1, hidden_dim)
        # router_logits: (batch * sequence_length, n_experts)
        router_logits = self.gate(hidden_states)

        routing_weights = ops.softmax(router_logits, dim=1, dtype=mindspore.float32)
        routing_weights, selected_experts = ops.topk(routing_weights, self.top_k, dim=-1)
        routing_weights = routing_weights / routing_weights.sum(axis=-1, keepdims=True)
        # we cast back to the input dtype
        routing_weights = routing_weights.to(hidden_states.dtype)

        final_hidden_states = ops.zeros(
            (batch_size * sequence_length, hidden_dim), dtype=hidden_states.dtype
        )

        # One hot encode the selected experts to create an expert mask
        # this will be used to easily index which expert is going to be sollicitated
        expert_mask = F.one_hot(selected_experts, self.num_experts).permute(2, 1, 0)

        # Loop over all available experts in the model and perform the computation on each expert
        for expert_idx in range(self.num_experts):
            expert_layer = self.experts[expert_idx]
            non_zero = ops.nonzero(expert_mask[expert_idx])
            idx, top_x = non_zero.tensor_split(2, 1)
            if top_x.shape[0] == 0:
                continue

            # Index the correct hidden states and compute the expert hidden state for
            # the current expert. We need to make sure to multiply the output hidden
            # states by `routing_weights` on the corresponding tokens (top-1 and top-2)
            current_state = hidden_states[None, top_x].reshape(-1, hidden_dim)
            current_hidden_states = expert_layer(current_state) * routing_weights[top_x, idx]

            # However `index_add_` only support torch tensors for indexing so we'll use
            # the `top_x` tensor here.
            final_hidden_states = final_hidden_states.index_add(0, top_x.astype(mindspore.int32).reshape(-1), current_hidden_states.to(hidden_states.dtype))
        final_hidden_states = final_hidden_states.reshape(batch_size, sequence_length, hidden_dim)
        return final_hidden_states, router_logits


class MixtralDecoderLayer(nn.Module):

    """
    This class represents a decoder layer for the Mixtral model, used for processing input sequences in
    neural network models. It includes functionality for self-attention, block sparse mixture of experts,
    layer normalization, and other operations specific to the Mixtral architecture.

    The MixtralDecoderLayer class inherits from nn.Module and contains methods for initialization and processing input
    data through the decoder layer. The __init__ method initializes the layer with configuration settings and creates
    necessary components such as self-attention mechanism, block sparse mixture of experts, and layer normalization.

    The forward method processes the input hidden states along with optional arguments like attention mask,
    position ids, past key values, and various output flags. It applies layer normalization, self-attention mechanism,
    block sparse mixture of experts, and additional layer normalization before returning the processed hidden states.
    Output can include attentions weights, present key values, and router logits based on the specified output flags.

    Please refer to the class code for detailed implementation and usage of the MixtralDecoderLayer.
    """
    def __init__(self, config: MixtralConfig, layer_idx: int):
        """
        Initializes an instance of MixtralDecoderLayer.

        Args:
            self (MixtralDecoderLayer): The instance of MixtralDecoderLayer.
            config (MixtralConfig): An instance of MixtralConfig containing configuration parameters for the layer.
            layer_idx (int): An integer representing the index of the layer.

        Returns:
            None.

        Raises:
            TypeError: If config is not an instance of MixtralConfig or if layer_idx is not an integer.
        """
        super().__init__()
        self.hidden_size = config.hidden_size

        self.self_attn = MIXTRAL_ATTENTION_CLASSES["eager"](config, layer_idx)

        self.block_sparse_moe = MixtralSparseMoeBlock(config)
        self.input_layernorm = MixtralRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = MixtralRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(
        self,
        hidden_states: mindspore.Tensor,
        attention_mask: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        past_key_value: Optional[Tuple[mindspore.Tensor]] = None,
        output_attentions: Optional[bool] = False,
        output_router_logits: Optional[bool] = False,
        use_cache: Optional[bool] = False,
    ) -> Tuple[mindspore.Tensor, Optional[Tuple[mindspore.Tensor, mindspore.Tensor]]]:
        """
        Args:
            hidden_states (`mindspore.Tensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
            attention_mask (`mindspore.Tensor`, *optional*): attention mask of size
                `(batch, sequence_length)` where padding elements are indicated by 0.
            past_key_value (`Tuple(mindspore.Tensor)`, *optional*): cached past key and value projection states
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
            output_router_logits (`bool`, *optional*):
                Whether or not to return the logits of all the routers. They are useful for computing the router loss, and
                should not be returned during inference.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
        """
        residual = hidden_states

        hidden_states = self.input_layernorm(hidden_states)

        # Self Attention
        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
        )
        hidden_states = residual + hidden_states

        # Fully Connected
        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states, router_logits = self.block_sparse_moe(hidden_states)
        hidden_states = residual + hidden_states

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        if use_cache:
            outputs += (present_key_value,)

        if output_router_logits:
            outputs += (router_logits,)

        return outputs


# Copied from transformers.models.mistral.modeling_mistral.MistralPreTrainedModel with Mistral->Mixtral
class MixtralPreTrainedModel(PreTrainedModel):

    """
    The `MixtralPreTrainedModel` class is a subclass of `PreTrainedModel` that represents a pre-trained model for
    Mixtral models.

    This class provides a method `_init_weights` that initializes the weights of the model. It takes a `cell`
    parameter and initializes the weights based on the type of the `cell`. If the `cell` is an instance of `nn.Linear`,
    the weight is initialized using the `Normal` initializer with a range specified by the `initializer_range` attribute
    of the `config` object. If the `cell` has a bias, it is initialized with zeros. If the `cell` is an instance of
    `nn.Embedding`, the weight is initialized with random values from a normal distribution with a mean of 0 and a
    standard deviation specified by the `initializer_range` attribute of the `config` object. If the `cell` has a
    `padding_idx`, the weight at the `padding_idx` is set to 0.

    Note:
        This docstring does not include signatures or any other code.
    """
    config_class = MixtralConfig
    base_model_prefix = "model"
    supports_gradient_checkpointing = True
    _no_split_modules = ["MixtralDecoderLayer"]
    _skip_keys_device_placement = "past_key_values"
    _supports_cache_class = True

    def _init_weights(self, cell):
        """Initialize the weights"""
        if isinstance(cell, nn.Linear):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            cell.weight.set_data(initializer(Normal(self.config.initializer_range),
                                                    cell.weight.shape, cell.weight.dtype))
            if cell.bias is not None:
                cell.bias.set_data(initializer('zeros', cell.bias.shape, cell.bias.dtype))
        elif isinstance(cell, nn.Embedding):
            weight = np.random.normal(0.0, self.config.initializer_range, cell.weight.shape)
            if cell.padding_idx:
                weight[cell.padding_idx] = 0

            cell.weight.set_data(Tensor(weight, cell.weight.dtype))


# Copied from transformers.models.mistral.modeling_mistral.MistralModel with MISTRAL->MIXTRAL,Mistral->Mixtral
class MixtralModel(MixtralPreTrainedModel):
    """
    Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`MixtralDecoderLayer`]

    Args:
        config: MixtralConfig
    """
    def __init__(self, config: MixtralConfig):
        """
        Initializes an instance of the MixtralModel class.

        Args:
            self: The instance of the class.
            config (MixtralConfig): The configuration object containing various parameters for the model.

        Returns:
            None

        Raises:
            None
        """
        super().__init__(config)
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size

        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size, self.padding_idx)
        self.layers = nn.ModuleList(
            [MixtralDecoderLayer(config, layer_idx) for layer_idx in range(config.num_hidden_layers)]
        )
        self.norm = MixtralRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

        self.gradient_checkpointing = False
        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        """
        Get the input embeddings for the MixtralModel.

        Args:
            self: The instance of the MixtralModel class.

        Returns:
            embed_tokens: This method returns the input embeddings for the MixtralModel.

        Raises:
            None.
        """
        return self.embed_tokens

    def set_input_embeddings(self, value):
        """
        Set the input embeddings for the MixtralModel.

        Args:
            self (MixtralModel): The instance of the MixtralModel class.
            value (Any): The input embeddings to be set for the model. It can be of any valid type.

        Returns:
            None.

        Raises:
            None.
        """
        self.embed_tokens = value

    def forward(
        self,
        input_ids: mindspore.Tensor = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        past_key_values: Optional[List[mindspore.Tensor]] = None,
        inputs_embeds: Optional[mindspore.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_router_logits: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, MoeModelOutputWithPast]:
        """
        Constructs the MixtralModel.

        Args:
            self: The object itself.
            input_ids (mindspore.Tensor, optional): The input tensor IDs. Default is None.
            attention_mask (mindspore.Tensor, optional): The attention mask tensor. Default is None.
            position_ids (mindspore.Tensor, optional): The position IDs tensor. Default is None.
            past_key_values (List[mindspore.Tensor], optional): The list of past key value tensors. Default is None.
            inputs_embeds (mindspore.Tensor, optional): The input embeddings tensor. Default is None.
            use_cache (bool, optional): Whether to use cache. Default is None.
            output_attentions (bool, optional): Whether to output attention tensors. Default is None.
            output_hidden_states (bool, optional): Whether to output hidden states. Default is None.
            output_router_logits (bool, optional): Whether to output router logits. Default is None.
            return_dict (bool, optional): Whether to return a dictionary. Default is None.

        Returns:
            Union[Tuple, MoeModelOutputWithPast]: The output of the MixtralModel, which can be a tuple or
                an instance of MoeModelOutputWithPast.

        Raises:
            ValueError: If both input_ids and inputs_embeds are specified.
            ValueError: If neither input_ids nor inputs_embeds are specified.
            Warning: If use_cache is True and gradient checkpointing is enabled.
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_router_logits = (
            output_router_logits if output_router_logits is not None else self.config.output_router_logits
        )
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # retrieve input_ids and inputs_embeds
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both decoder_input_ids and decoder_inputs_embeds at the same time")
        if input_ids is not None:
            batch_size, seq_length = input_ids.shape
        elif inputs_embeds is not None:
            batch_size, seq_length, _ = inputs_embeds.shape
        else:
            raise ValueError("You have to specify either decoder_input_ids or decoder_inputs_embeds")

        past_key_values_length = 0

        if self.gradient_checkpointing and self.training:
            if use_cache:
                logger.warning_once(
                    "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                )
                use_cache = False

        if use_cache:
            use_legacy_cache = not isinstance(past_key_values, Cache)
            if use_legacy_cache:
                past_key_values = DynamicCache.from_legacy_cache(past_key_values)
            past_key_values_length = past_key_values.get_usable_length(seq_length)

        if position_ids is None:
            position_ids = ops.arange(
                past_key_values_length, seq_length + past_key_values_length, dtype=mindspore.int64
            )
            position_ids = position_ids.unsqueeze(0).view(-1, seq_length)
        else:
            position_ids = position_ids.view(-1, seq_length).long()

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        # 4d mask is passed through the layers
        attention_mask = _prepare_4d_causal_attention_mask(
            attention_mask,
            (batch_size, seq_length),
            inputs_embeds,
            past_key_values_length,
            sliding_window=self.config.sliding_window,
        )

        hidden_states = inputs_embeds

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        all_router_logits = () if output_router_logits else None
        next_decoder_cache = None

        for decoder_layer in self.layers:
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            layer_outputs = decoder_layer(
                hidden_states,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_value=past_key_values,
                output_attentions=output_attentions,
                output_router_logits=output_router_logits,
                use_cache=use_cache,
            )

            hidden_states = layer_outputs[0]
            if use_cache:
                next_decoder_cache = layer_outputs[2 if output_attentions else 1]

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

            if output_router_logits:
                all_router_logits += (layer_outputs[-1],)

        hidden_states = self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        next_cache = None
        if use_cache:
            next_cache = next_decoder_cache.to_legacy_cache() if use_legacy_cache else next_decoder_cache

        if not return_dict:
            return tuple(
                v
                for v in [hidden_states, next_cache, all_hidden_states, all_self_attns, all_router_logits]
                if v is not None
            )
        return MoeModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=next_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
            router_logits=all_router_logits,
        )


class MixtralForCausalLM(MixtralPreTrainedModel):
    """
    Represents a Mixtral model for causal language modeling.

    This class provides methods for initializing the model, setting and getting input and output embeddings,
    setting and getting the decoder, forwarding the model, preparing inputs for generation, and reordering
    cache values.

    The class inherits from MixtralPreTrainedModel.
    The class also includes a detailed example demonstrating the usage of the MixtralForCausalLM model for
    generating text.
    """
    _tied_weights_keys = ["lm_head.weight"]

    def __init__(self, config):
        """
        Initializes an instance of the MixtralForCausalLM class.

        Args:
            self: The instance of the class.
            config:
                A dictionary containing configuration parameters for the model.

                - Type: dict
                - Purpose: Specifies the configuration settings for the model.
                - Restrictions: Must be a valid dictionary object.

        Returns:
            None.

        Raises:
            None.
        """
        super().__init__(config)
        self.model = MixtralModel(config)
        self.vocab_size = config.vocab_size
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)
        self.router_aux_loss_coef = config.router_aux_loss_coef
        self.num_experts = config.num_local_experts
        self.num_experts_per_tok = config.num_experts_per_tok
        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        """Retrieve input embeddings from the model.

        Args:
            self (MixtralForCausalLM): The instance of the MixtralForCausalLM class.

        Returns:
            None: This method returns the input embeddings from the model.

        Raises:
            None.
        """
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        """
        Sets the input embeddings of the MixtralForCausalLM model.

        Args:
            self (MixtralForCausalLM): The instance of the MixtralForCausalLM class.
            value (object): The new input embeddings to be set for the model.
                It can be of any compatible type that can be assigned to the 'embed_tokens' attribute of the model.

        Returns:
            None: This method updates the 'embed_tokens' attribute of the model in place.

        Raises:
            None.
        """
        self.model.embed_tokens = value

    def get_output_embeddings(self):
        """
        Retrieve the output embeddings from the MixtralForCausalLM model.

        Args:
            self: An instance of the MixtralForCausalLM class.

        Returns:
            The output embeddings of the model.

        Raises:
            None.

        This method retrieves the output embeddings from the MixtralForCausalLM model.
        The output embeddings represent the learned representations of the model's output tokens.
        These embeddings can be used for downstream tasks such as fine-tuning or further analysis.
        """
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        """
        Set the output embeddings of the MixtralForCausalLM model.

        Args:
            self (MixtralForCausalLM): The instance of the MixtralForCausalLM model.
            new_embeddings (object): The new output embeddings to be set for the model.
                Should be compatible with the model's architecture and dimensions.

        Returns:
            None.

        Raises:
            None.
        """
        self.lm_head = new_embeddings

    def set_decoder(self, decoder):
        """
        Sets the decoder for MixtralForCausalLM.

        Args:
            self (MixtralForCausalLM): The instance of MixtralForCausalLM.
            decoder: The decoder object to be set for the model.

        Returns:
            None.

        Raises:
            None.
        """
        self.model = decoder

    def get_decoder(self):
        """
        Method to retrieve the decoder from the MixtralForCausalLM model.

        Args:
            self (MixtralForCausalLM): The instance of MixtralForCausalLM class.
                This parameter is required to access the model.
                It should be an instance of the MixtralForCausalLM class.

        Returns:
            None: This method returns None as it simply retrieves and returns the model's decoder.

        Raises:
            None.
        """
        return self.model

    def forward(
        self,
        input_ids: mindspore.Tensor = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        past_key_values: Optional[List[mindspore.Tensor]] = None,
        inputs_embeds: Optional[mindspore.Tensor] = None,
        labels: Optional[mindspore.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_router_logits: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, MoeCausalLMOutputWithPast]:
        r"""
        Args:
            labels (`mindspore.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
                Labels for computing the masked language modeling loss. Indices should either be in `[0, ...,
                config.vocab_size]` or -100 (see `input_ids` docstring). Tokens with indices set to `-100` are ignored
                (masked), the loss is only computed for the tokens with labels in `[0, ..., config.vocab_size]`.

        Returns:
            Union[Tuple, MoeCausalLMOutputWithPast]

        Example:
            ```python
            >>> from transformers import AutoTokenizer, MixtralForCausalLM
            ...
            >>> model = MixtralForCausalLM.from_pretrained("mistralai/Mixtral-8x7B-v0.1")
            >>> tokenizer = AutoTokenizer.from_pretrained("mistralai/Mixtral-8x7B-v0.1")
            ...
            >>> prompt = "Hey, are you conscious? Can you talk to me?"
            >>> inputs = tokenizer(prompt, return_tensors="pt")
            ...
            >>> # Generate
            >>> generate_ids = model.generate(inputs.input_ids, max_length=30)
            >>> tokenizer.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
            "Hey, are you conscious? Can you talk to me?\nI'm not conscious, but I can talk to you."
            ```
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_router_logits = (
            output_router_logits if output_router_logits is not None else self.config.output_router_logits
        )

        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            output_router_logits=output_router_logits,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]
        logits = self.lm_head(hidden_states)
        logits = logits.float()

        loss = None
        if labels is not None:
            # Shift so that tokens < n predict n
            shift_logits = logits[..., :-1, :]
            shift_labels = labels[..., 1:]
            # Flatten the tokens
            shift_logits = shift_logits.view(-1, self.config.vocab_size)
            shift_labels = shift_labels.view(-1)
            # Enable model parallelism
            loss = F.cross_entropy(shift_logits, shift_labels)

        aux_loss = None
        if output_router_logits:
            aux_loss = load_balancing_loss_func(
                outputs.router_logits if return_dict else outputs[-1],
                self.num_experts,
                self.num_experts_per_tok,
                attention_mask,
            )
            if labels is not None:
                loss += self.router_aux_loss_coef * aux_loss

        if not return_dict:
            output = (logits,) + outputs[1:]
            if output_router_logits:
                output = (aux_loss,) + output
            return (loss,) + output if loss is not None else output

        return MoeCausalLMOutputWithPast(
            loss=loss,
            aux_loss=aux_loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            router_logits=outputs.router_logits,
        )

    def prepare_inputs_for_generation(
        self,
        input_ids,
        past_key_values=None,
        attention_mask=None,
        inputs_embeds=None,
        output_router_logits=False,
        **kwargs,
    ):
        """
        Prepare inputs for generation in the MixtralForCausalLM class.

        Args:
            self (object): The instance of the MixtralForCausalLM class.
            input_ids (mindspore.Tensor): The input tensor containing tokenized input IDs.
            past_key_values (Cache or tuple or None): The past key values for autoregressive generation or
                None if no past values are available.
            attention_mask (mindspore.Tensor or None): The attention mask tensor to avoid attending to padding tokens,
                or None if no mask is provided.
            inputs_embeds (mindspore.Tensor or None): The input embeddings tensor, or None if input_ids is used for embeddings.
            output_router_logits (bool): A flag indicating whether to output router logits for routing the generated tokens.

        Returns:
            dict: A dictionary containing the model inputs for generation, including input_ids, position_ids,
                past_key_values, use_cache, attention_mask, and output_router_logits.

        Raises:
            ValueError: If the input_ids and attention_mask dimensions are inconsistent or if the cache length
                exceeds the maximum length.
            TypeError: If the past_key_values type is invalid.
            IndexError: If the input_ids shape is invalid.
        """
        # Omit tokens covered by past_key_values
        if past_key_values is not None:
            if isinstance(past_key_values, Cache):
                cache_length = past_key_values.get_seq_length()
                past_length = past_key_values.seen_tokens
                max_cache_length = past_key_values.get_max_length()
            else:
                cache_length = past_length = past_key_values[0][0].shape[2]
                max_cache_length = None

            # Keep only the unprocessed tokens:
            # 1 - If the length of the attention_mask exceeds the length of input_ids, then we are in a setting where
            # some of the inputs are exclusively passed as part of the cache (e.g. when passing input_embeds as
            # input)
            if attention_mask is not None and attention_mask.shape[1] > input_ids.shape[1]:
                input_ids = input_ids[:, -(attention_mask.shape[1] - past_length) :]
            # 2 - If the past_length is smaller than input_ids', then input_ids holds all input tokens. We can discard
            # input_ids based on the past_length.
            elif past_length < input_ids.shape[1]:
                input_ids = input_ids[:, past_length:]
            # 3 - Otherwise (past_length >= input_ids.shape[1]), let's assume input_ids only has unprocessed tokens.

            # If we are about to go beyond the maximum cache length, we need to crop the input attention mask.
            if (
                max_cache_length is not None
                and attention_mask is not None
                and cache_length + input_ids.shape[1] > max_cache_length
            ):
                attention_mask = attention_mask[:, -max_cache_length:]

        position_ids = kwargs.get("position_ids", None)
        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.int().cumsum(-1) - 1
            position_ids = position_ids.masked_fill(attention_mask == 0, 1)
            if past_key_values:
                position_ids = position_ids[:, -input_ids.shape[1] :]

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs.update(
            {
                "position_ids": position_ids,
                "past_key_values": past_key_values,
                "use_cache": kwargs.get("use_cache"),
                "attention_mask": attention_mask,
                "output_router_logits": output_router_logits,
            }
        )
        return model_inputs

    @staticmethod
    def _reorder_cache(past_key_values, beam_idx):
        """
        Reorders the cache for each layer in the MixtralForCausalLM class based on the provided beam index.

        Args:
            past_key_values (tuple): A tuple of past key values for each layer in the model.
                Each element in the tuple represents the past key values for a single layer,
                and is itself a tuple of tensors.
            beam_idx (mindspore.Tensor): A tensor containing the beam indices.

        Returns:
            tuple: The reordered past key values for each layer.
                Each element in the tuple represents the reordered past key values for a single layer,
                and is itself a tuple of tensors.

        Raises:
            None.

        Note:
            This method is a static method, which means it can be called on the class itself
            without creating an instance of the class.

        Example:
            ```python
            >>> past_key_values = ((tensor([[1, 2, 3]]), tensor([[4, 5, 6]]))),
            (tensor([[7, 8, 9]]), tensor([[10, 11, 12]]))))
            >>> beam_idx = tensor([1, 0])
            >>> reordered_past = MixtralForCausalLM._reorder_cache(past_key_values, beam_idx)
            >>> print(reordered_past)
            ((tensor([[4, 5, 6]]), tensor([[1, 2, 3]]))),
             (tensor([[10, 11, 12]]), tensor([[7, 8, 9]]))))
             ```
        """
        reordered_past = ()
        for layer_past in past_key_values:
            reordered_past += (
                tuple(past_state.index_select(0, beam_idx) for past_state in layer_past),
            )
        return reordered_past


# Copied from transformers.models.llama.modeling_llama.LlamaForSequenceClassification with Llama->Mixtral, LLAMA->MIXTRAL
class MixtralForSequenceClassification(MixtralPreTrainedModel):

    """
    MixtralForSequenceClassification

    This class represents a Mixtral model for sequence classification. It inherits from MixtralPreTrainedModel and
    is designed to handle sequence classification tasks. It includes methods for initializing the model, getting and
    setting input embeddings, and forwarding the model for sequence classification.
    The class also provides detailed documentation for the forward method, which accepts various input parameters and
    returns the sequence classification output.

    Attributes:
        num_labels: An integer representing the number of labels for sequence classification.
        model: An instance of MixtralModel used for the sequence classification task.
        score: A neural network module for generating scores based on hidden states.

    Methods:
        __init__: Initializes the MixtralForSequenceClassification instance with the provided configuration.
        get_input_embeddings: Retrieves the input embeddings from the model.
        set_input_embeddings: Sets the input embeddings for the model.
        forward: Constructs the model for sequence classification, processing the input data and returning the
            sequence classification output.

    The forward method supports various optional input parameters, including input_ids, attention_mask, position_ids,
    past_key_values, inputs_embeds, labels, use_cache, output_attentions, output_hidden_states, and return_dict.
    The labels parameter is optional and can be used for computing the sequence classification/regression loss.
    The method also handles different problem types such as regression, single-label classification, and multi-label
    classification, and computes the loss accordingly.

    Returns:
        Conditional returns:

            - When return_dict is False, the forward method returns a tuple containing the loss and other sequence
            classifier outputs.
            - When return_dict is True, it returns a SequenceClassifierOutputWithPast object that
            includes the loss, logits, past_key_values, hidden_states, and attentions.

    Note:
        The class documentation and method descriptions are based on the provided Python code and its associated functionality.
    """
    def __init__(self, config):
        """
        Initializes an instance of MixtralForSequenceClassification class.

        Args:
            self: The object instance itself.
            config (object): An object containing configuration settings for the model.
                It should have a 'num_labels' attribute specifying the number of output labels.

        Returns:
            None.

        Raises:
            AttributeError: If the 'config' parameter does not contain the required 'num_labels' attribute.
            TypeError: If the 'config' parameter is not of the expected type.
            ValueError: If there are issues during the initialization process.
        """
        super().__init__(config)
        self.num_labels = config.num_labels
        self.model = MixtralModel(config)
        self.score = nn.Linear(config.hidden_size, self.num_labels, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        """
        Method: get_input_embeddings

        Description:
        This method retrieves the input embeddings from the model.

        Args:
            self: An instance of the MixtralForSequenceClassification class.

        Returns:
            None: This method does not return any value.

        Raises:
            None
        """
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        """
        Set the input embeddings for the MixtralForSequenceClassification model.

        Args:
            self (MixtralForSequenceClassification): The instance of the MixtralForSequenceClassification class.
            value (mindspore.Tensor): The input embeddings to be set for the model. It should be of type mindspore.Tensor.

        Returns:
            None.

        Raises:
            None.
        """
        self.model.embed_tokens = value

    def forward(
        self,
        input_ids: mindspore.Tensor = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        past_key_values: Optional[List[mindspore.Tensor]] = None,
        inputs_embeds: Optional[mindspore.Tensor] = None,
        labels: Optional[mindspore.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, SequenceClassifierOutputWithPast]:
        r"""
        Args:
            labels (`mindspore.Tensor` of shape `(batch_size,)`, *optional*):
                Labels for computing the sequence classification/regression loss. Indices should be in `[0, ...,
                config.num_labels - 1]`. If `config.num_labels == 1` a regression loss is computed (Mean-Square loss), If
                `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.model(
            input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        hidden_states = transformer_outputs[0]
        logits = self.score(hidden_states)

        if input_ids is not None:
            batch_size = input_ids.shape[0]
        else:
            batch_size = inputs_embeds.shape[0]

        if self.config.pad_token_id is None and batch_size != 1:
            raise ValueError("Cannot handle batch sizes > 1 if no padding token is defined.")
        if self.config.pad_token_id is None:
            sequence_lengths = -1
        else:
            if input_ids is not None:
                # if no pad token found, use modulo instead of reverse indexing for ONNX compatibility
                sequence_lengths = ops.eq(input_ids, self.config.pad_token_id).int().argmax(-1) - 1
                sequence_lengths = sequence_lengths % input_ids.shape[-1]
            else:
                sequence_lengths = -1

        pooled_logits = logits[ops.arange(batch_size), sequence_lengths]

        loss = None
        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and labels.dtype in (mindspore.int64, mindspore.int32):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"

            if self.config.problem_type == "regression":
                if self.num_labels == 1:
                    loss = F.mse_loss(pooled_logits.squeeze(), labels.squeeze())
                else:
                    loss = F.mse_loss(pooled_logits, labels)
            elif self.config.problem_type == "single_label_classification":
                loss = F.cross_entropy(pooled_logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss = F.binary_cross_entropy_with_logits(pooled_logits, labels)
        if not return_dict:
            output = (pooled_logits,) + transformer_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return SequenceClassifierOutputWithPast(
            loss=loss,
            logits=pooled_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )

class MixtralForTokenClassification(MixtralPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.model = MixtralModel(config)
        if getattr(config, "classifier_dropout", None) is not None:
            classifier_dropout = config.classifier_dropout
        elif getattr(config, "hidden_dropout", None) is not None:
            classifier_dropout = config.hidden_dropout
        else:
            classifier_dropout = 0.1
        self.dropout = nn.Dropout(classifier_dropout)
        self.score = nn.Linear(config.hidden_size, config.num_labels)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        self.model.embed_tokens = value

    def forward(
        self,
        input_ids: Optional[mindspore.Tensor] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        past_key_values: Optional[List[mindspore.Tensor]] = None,
        inputs_embeds: Optional[mindspore.Tensor] = None,
        labels: Optional[mindspore.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, TokenClassifierOutput]:
        r"""
        labels (`mindspore.Tensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the sequence classification/regression loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels == 1` a regression loss is computed (Mean-Square loss), If
            `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.model(
            input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        sequence_output = outputs[0]
        sequence_output = self.dropout(sequence_output)
        logits = self.score(sequence_output)

        loss = None
        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))

        if not return_dict:
            output = (logits,) + outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return TokenClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

__all__ = [
    "MixtralForCausalLM",
    "MixtralModel",
    "MixtralPreTrainedModel",
    "MixtralForSequenceClassification",
    "MixtralForTokenClassification"
]
