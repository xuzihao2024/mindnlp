# coding=utf-8
# Copyright 2023 The HuggingFace Team. All rights reserved.
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
#

import math
import unittest
import numpy as np

from mindnlp.transformers import MptConfig
from mindnlp.utils.testing_utils import require_mindspore, slow, is_mindspore_available

from ...generation.test_utils import GenerationTesterMixin
from ...test_configuration_common import ConfigTester
from ...test_modeling_common import ModelTesterMixin, ids_tensor, random_attention_mask


if is_mindspore_available():
    import mindspore
    from mindspore import ops

    from mindnlp.transformers import (
        AutoTokenizer,
        MptForCausalLM,
        MptForQuestionAnswering,
        MptForSequenceClassification,
        MptForTokenClassification,
        MptModel,
    )


@require_mindspore
class MptModelTester:
    def __init__(
        self,
        parent,
        batch_size=14,
        seq_length=7,
        is_training=True,
        use_token_type_ids=False,
        use_input_mask=True,
        use_labels=True,
        use_mc_token_ids=True,
        vocab_size=99,
        hidden_size=48,
        num_hidden_layers=2,
        num_attention_heads=4,
        intermediate_size=37,
        hidden_act="gelu",
        hidden_dropout_prob=0.1,
        attention_dropout_prob=0.1,
        max_position_embeddings=512,
        type_vocab_size=16,
        type_sequence_label_size=2,
        initializer_range=0.02,
        num_labels=3,
        num_choices=4,
    ):
        self.parent = parent
        self.batch_size = batch_size
        self.seq_length = seq_length
        self.is_training = is_training
        self.use_token_type_ids = use_token_type_ids
        self.use_input_mask = use_input_mask
        self.use_labels = use_labels
        self.use_mc_token_ids = use_mc_token_ids
        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.intermediate_size = intermediate_size
        self.hidden_act = hidden_act
        self.hidden_dropout_prob = hidden_dropout_prob
        self.attention_dropout_prob = attention_dropout_prob
        self.max_position_embeddings = max_position_embeddings
        self.type_vocab_size = type_vocab_size
        self.type_sequence_label_size = type_sequence_label_size
        self.initializer_range = initializer_range
        self.num_labels = num_labels
        self.num_choices = num_choices
        self.scope = None
        self.bos_token_id = vocab_size - 1
        self.eos_token_id = vocab_size - 1
        self.pad_token_id = vocab_size - 1

    def get_large_model_config(self):
        return MptConfig.from_pretrained("mosaicml/mpt-7b")

    def prepare_config_and_inputs(self, gradient_checkpointing=False):
        input_ids = ids_tensor([self.batch_size, self.seq_length], self.vocab_size)

        input_mask = None
        if self.use_input_mask:
            input_mask = random_attention_mask([self.batch_size, self.seq_length])

        sequence_labels = None
        if self.use_labels:
            sequence_labels = ids_tensor([self.batch_size], self.type_sequence_label_size)

        config = self.get_config(gradient_checkpointing=gradient_checkpointing)

        return (config, input_ids, input_mask, sequence_labels)

    def get_config(self, gradient_checkpointing=False):
        return MptConfig(
            vocab_size=self.vocab_size,
            seq_length=self.seq_length,
            hidden_size=self.hidden_size,
            n_layers=self.num_hidden_layers,
            n_heads=self.num_attention_heads,
            hidden_dropout=self.hidden_dropout_prob,
            attention_dropout=self.attention_dropout_prob,
            n_positions=self.max_position_embeddings,
            type_vocab_size=self.type_vocab_size,
            initializer_range=self.initializer_range,
            use_cache=True,
            bos_token_id=self.bos_token_id,
            eos_token_id=self.eos_token_id,
            pad_token_id=self.pad_token_id,
            num_labels=self.num_labels,
            gradient_checkpointing=gradient_checkpointing,
            dtype="float32",
        )

    def create_and_check_mpt_model(self, config, input_ids, input_mask, *args):
        model = MptModel(config=config)
        model.set_train(False)

        result = model(input_ids)

        self.parent.assertEqual(result.last_hidden_state.shape, (self.batch_size, self.seq_length, self.hidden_size))
        self.parent.assertEqual(len(result.past_key_values), config.n_layers)

    def create_and_check_mpt_model_past(self, config, input_ids, input_mask, *args):
        model = MptModel(config=config)
        model.set_train(False)

        # first forward pass
        outputs = model(input_ids, attention_mask=ops.ones_like(input_ids), use_cache=True)
        outputs_use_cache_conf = model(input_ids, attention_mask=ops.ones_like(input_ids))
        outputs_no_past = model(input_ids, use_cache=False, attention_mask=ops.ones_like(input_ids))

        self.parent.assertTrue(len(outputs) == len(outputs_use_cache_conf))
        self.parent.assertTrue(len(outputs) == len(outputs_no_past) + 1)

        past = outputs["past_key_values"]

        # create hypothetical next token and extent to next_input_ids
        next_tokens = ids_tensor((self.batch_size, 1), config.vocab_size)

        # append to next input_ids and token_type_ids
        next_input_ids = ops.cat([input_ids, next_tokens], axis=-1)

        output_from_no_past = model(next_input_ids)["last_hidden_state"]
        output_from_past = model(next_tokens, past_key_values=past)["last_hidden_state"]

        # select random slice
        random_slice_idx = ids_tensor((1,), output_from_past.shape[-1]).item()
        output_from_no_past_slice = output_from_no_past[:, -1, random_slice_idx]
        output_from_past_slice = output_from_past[:, 0, random_slice_idx]

        # test that outputs are equal for slice
        self.parent.assertTrue(np.allclose(output_from_past_slice.asnumpy(), output_from_no_past_slice.asnumpy(), atol=1e-3))

    def create_and_check_mpt_model_attention_mask_past(self, config, input_ids, input_mask, *args):
        model = MptModel(config=config)
        model.set_train(False)

        # create attention mask
        attn_mask = ops.ones(input_ids.shape, dtype=mindspore.int64)
        half_seq_length = self.seq_length // 2
        attn_mask[:, half_seq_length:] = 0

        # first forward pass
        output, past = model(input_ids, attention_mask=attn_mask).to_tuple()

        # create hypothetical next token and extent to next_input_ids
        next_tokens = ids_tensor((self.batch_size, 1), config.vocab_size)

        # change a random masked slice from input_ids
        random_seq_idx_to_change = ids_tensor((1,), half_seq_length).item() + 1
        random_other_next_tokens = ids_tensor((self.batch_size, 1), config.vocab_size).squeeze(-1)
        input_ids[:, -random_seq_idx_to_change] = random_other_next_tokens

        # append to next input_ids and attn_mask
        next_input_ids = ops.cat([input_ids, next_tokens], axis=-1)
        attn_mask = ops.cat(
            [attn_mask, ops.ones((attn_mask.shape[0], 1), dtype=mindspore.int64)],
            axis=1,
        )

        # get two different outputs
        output_from_no_past = model(next_input_ids, attention_mask=attn_mask)["last_hidden_state"]
        output_from_past = model(next_tokens, past_key_values=past, attention_mask=attn_mask)["last_hidden_state"]

        # select random slice
        random_slice_idx = ids_tensor((1,), output_from_past.shape[-1]).item()
        output_from_no_past_slice = output_from_no_past[:, -1, random_slice_idx]
        output_from_past_slice = output_from_past[:, 0, random_slice_idx]

        # test that outputs are equal for slice
        self.parent.assertTrue(np.allclose(output_from_past_slice.asnumpy(), output_from_no_past_slice.asnumpy(), atol=1e-3))

    def create_and_check_mpt_model_past_large_inputs(self, config, input_ids, input_mask, *args):
        model = MptModel(config=config)
        model.set_train(False)

        # first forward pass
        outputs = model(
            input_ids,
            attention_mask=input_mask,
            use_cache=True,
        )
        past_key_values = outputs.past_key_values

        # create hypothetical multiple next token and extent to next_input_ids
        next_tokens = ids_tensor((self.batch_size, 3), config.vocab_size)
        next_mask = ids_tensor((self.batch_size, 3), vocab_size=2)

        # append to next input_ids and
        next_input_ids = ops.cat([input_ids, next_tokens], axis=-1)
        next_attention_mask = ops.cat([input_mask, next_mask], axis=-1)

        output_from_no_past = model(
            next_input_ids,
            attention_mask=next_attention_mask,
            output_hidden_states=True,
        )
        hidden_states_from_no_past = output_from_no_past["hidden_states"][0]

        output_from_past = model(
            next_tokens,
            attention_mask=next_attention_mask,
            past_key_values=past_key_values,
            output_hidden_states=True,
        )
        hidden_states_from_past = output_from_past["hidden_states"][0]

        # select random slice
        random_slice_idx = ids_tensor((1,), hidden_states_from_past.shape[-1]).item()
        output_from_no_past_slice = hidden_states_from_no_past[:, -3:, random_slice_idx]
        output_from_past_slice = hidden_states_from_past[:, :, random_slice_idx]

        self.parent.assertTrue(output_from_past_slice.shape[1] == next_tokens.shape[1])

        # test that outputs are equal for slice
        self.parent.assertTrue(np.allclose(output_from_past_slice.asnumpy(), output_from_no_past_slice.asnumpy(), atol=1e-3))

    def create_and_check_lm_head_model(self, config, input_ids, input_mask, *args):
        model = MptForCausalLM(config)
        model.set_train(False)

        result = model(input_ids, labels=input_ids)
        self.parent.assertEqual(result.loss.shape, ())
        self.parent.assertEqual(result.logits.shape, (self.batch_size, self.seq_length, self.vocab_size))

    def create_and_check_sequence_classification_model(self, config, input_ids, input_mask, *args):
        config.num_labels = self.num_labels
        model = MptForSequenceClassification(config)
        model.set_train(False)

        result = model(input_ids, attention_mask=input_mask)
        self.parent.assertEqual(result.logits.shape, (self.batch_size, self.num_labels))

    def create_and_check_token_classification_model(self, config, input_ids, input_mask, *args):
        model = MptForTokenClassification(config)
        model.set_train(False)

        result = model(input_ids, attention_mask=input_mask)
        self.parent.assertEqual(result.logits.shape, (self.batch_size, self.seq_length, self.num_labels))

    def create_and_check_question_answering_model(self, config, input_ids, input_mask, *args):
        model = MptForQuestionAnswering(config)
        model.set_train(False)

        result = model(input_ids, attention_mask=input_mask)
        self.parent.assertEqual(result.logits.shape, (self.batch_size, self.seq_length, self.num_labels))

    def create_and_check_forward_and_backwards(
        self, config, input_ids, input_mask, *args, gradient_checkpointing=False
    ):
        model = MptForCausalLM(config)

        result = model(input_ids, labels=input_ids)
        self.parent.assertEqual(result.loss.shape, ())
        self.parent.assertEqual(result.logits.shape, (self.batch_size, self.seq_length, self.vocab_size))

    def create_and_check_mpt_weight_initialization(self, config, *args):
        model = MptModel(config)
        model_std = model.config.initializer_range / math.sqrt(2 * model.config.n_layers)
        for key in model.parameters_dict().keys():
            if "c_proj" in key and "weight" in key:
                self.parent.assertLessEqual(abs(ops.std(model.parameters_dict()[key]) - model_std), 0.001)
                self.parent.assertLessEqual(abs(ops.mean(model.parameters_dict()[key]) - 0.0), 0.01)

    def prepare_config_and_inputs_for_common(self):
        config_and_inputs = self.prepare_config_and_inputs()

        config, input_ids, input_mask, sequence_labels = config_and_inputs

        inputs_dict = {"input_ids": input_ids}

        return config, inputs_dict


class MptConfigTester(ConfigTester):
    def __init__(self, parent, config_class=None, has_text_modality=True, common_properties=None, **kwargs):
        super().__init__(parent, config_class, has_text_modality, common_properties, **kwargs)

    def test_attn_config_as_dict(self):
        config = self.config_class(**self.inputs_dict, attn_config={"attn_impl": "flash", "softmax_scale": None})
        self.parent.assertTrue(config.attn_config.attn_impl == "flash")
        self.parent.assertTrue(config.attn_config.softmax_scale is None)

    def run_common_tests(self):
        self.test_attn_config_as_dict()
        return super().run_common_tests()


@require_mindspore
class MptModelTest(ModelTesterMixin, GenerationTesterMixin, unittest.TestCase):
    all_model_classes = (
        (
            MptForCausalLM,
            MptForSequenceClassification,
            MptForTokenClassification,
            MptForQuestionAnswering,
        )
        if is_mindspore_available()
        else ()
    )

    all_generative_model_classes = (MptForCausalLM,) if is_mindspore_available() else ()
    fx_compatible = False
    test_missing_keys = False
    test_pruning = False
    test_torchscript = False
    test_head_masking = False
    pipeline_model_mapping = (
        {
            "feature-extraction": MptModel,
            "question-answering": MptForQuestionAnswering,
            "text-classification": MptForSequenceClassification,
            "text-generation": MptForCausalLM,
            "token-classification": MptForTokenClassification,
            "zero-shot": MptForSequenceClassification,
        }
        if is_mindspore_available()
        else {}
    )

    def setUp(self):
        self.model_tester = MptModelTester(self)
        self.config_tester = MptConfigTester(self, config_class=MptConfig, n_embd=37)

    def test_config(self):
        self.config_tester.run_common_tests()

    def test_mpt_model(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_mpt_model(*config_and_inputs)

    def test_mpt_model_alibi_tensor(self):
        # test creation of alibi tensor when num heads is not a power of two
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        config_and_inputs[0].n_heads = 6
        self.model_tester.create_and_check_mpt_model(*config_and_inputs)

    def test_mpt_model_past(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_mpt_model_past(*config_and_inputs)

    def test_mpt_model_att_mask_past(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_mpt_model_attention_mask_past(*config_and_inputs)

    def test_mpt_model_past_large_inputs(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_mpt_model_past_large_inputs(*config_and_inputs)

    def test_mpt_lm_head_model(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_lm_head_model(*config_and_inputs)

    def test_mpt_sequence_classification_model(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_sequence_classification_model(*config_and_inputs)

    def test_mpt_token_classification_model(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_token_classification_model(*config_and_inputs)

    def test_mpt_gradient_checkpointing(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_forward_and_backwards(*config_and_inputs, gradient_checkpointing=True)

    def test_mpt_weight_initialization(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_mpt_weight_initialization(*config_and_inputs)

    @unittest.skip("For backward compatibility the lm_head is not in the model's state dict on the Hub.")
    def test_model_weights_reload_no_missing_tied_weights(self):
        pass

    @slow
    def test_model_from_pretrained(self):
        model_name = "mosaicml/mpt-7b"
        model = MptModel.from_pretrained(model_name)
        self.assertIsNotNone(model)


@slow
class MptIntegrationTests(unittest.TestCase):
    def test_generation_8k(self):
        model_id = "mosaicml/mpt-7b-8k"
        tokenizer = AutoTokenizer.from_pretrained(model_id)

        # Load in 4bit to fit the daily CI runner GPU RAM
        model = MptForCausalLM.from_pretrained(
            model_id, torch_dtype=mindspore.float16
        )

        input_text = "Hello"
        expected_output = 'Hello, I\'m a new user of the forum. I have a question about the "Safety"'

        inputs = tokenizer(input_text, return_tensors="ms")
        outputs = model.generate(**inputs, max_new_tokens=20)

        decoded_output = tokenizer.decode(outputs[0], skip_special_tokens=True)
        self.assertEqual(decoded_output, expected_output)

    def test_generation(self):
        model_id = "mosaicml/mpt-7b"
        tokenizer = AutoTokenizer.from_pretrained(model_id)

        # Load in 4bit to fit the daily CI runner GPU RAM
        model = MptForCausalLM.from_pretrained(
            model_id, torch_dtype=mindspore.float16
        )

        input_text = "Hello"
        expected_output = (
            "Hello and welcome to the first day of the new release countdown for the month of May!\nToday"
        )

        inputs = tokenizer(input_text, return_tensors="ms")
        outputs = model.generate(**inputs, max_new_tokens=20)

        decoded_output = tokenizer.decode(outputs[0], skip_special_tokens=True)
        self.assertEqual(decoded_output, expected_output)

    def test_generation_batched(self):
        model_id = "mosaicml/mpt-7b"
        tokenizer = AutoTokenizer.from_pretrained(model_id)

        # Load in 4bit to fit the daily CI runner GPU RAM
        model = MptForCausalLM.from_pretrained(
            model_id, torch_dtype=mindspore.float16
        )

        input_texts = ["Hello my name is", "Today I am going at the gym and"]
        tokenizer.pad_token_id = tokenizer.eos_token_id
        tokenizer.padding_side = "left"

        inputs = tokenizer(input_texts, return_tensors="ms", padding=True)

        expected_output = [
            "Hello my name is Tiffany and I am a mother of two beautiful children. I have been a nanny for over",
            "Today I am going at the gym and then I am going to go to the grocery store and get some food. I am going to make",
        ]
        outputs = model.generate(**inputs, max_new_tokens=20)

        decoded_outputs = tokenizer.batch_decode(outputs, skip_special_tokens=True)
        for i, predicted_output in enumerate(decoded_outputs):
            self.assertEqual(predicted_output, expected_output[i])

    def test_model_logits(self):
        model_id = "mosaicml/mpt-7b"

        # Load in 4bit to fit the daily CI runner GPU RAM
        model = MptForCausalLM.from_pretrained(
            model_id, torch_dtype=mindspore.float16
        )

        dummy_input = mindspore.Tensor([[1, 2, 3, 4, 5]])

        outputs = model(dummy_input, output_hidden_states=True)

        expected_slice = mindspore.Tensor([-0.2539, -0.2178, -0.1953]).to(mindspore.float16)
        predicted_slice = outputs.hidden_states[-1][0, 0, :3]

        self.assertTrue(np.allclose(expected_slice.asnumpy(), predicted_slice.asnumpy(), atol=1e-3, rtol=1e-3))