import copy
from typing import Optional, List, Union, Tuple

from transformers import MBartForCausalLM, MBartConfig
from torch import nn
from transformers.activations import ACT2FN
from transformers.modeling_attn_mask_utils import _prepare_4d_causal_attention_mask, _prepare_4d_attention_mask
from transformers.modeling_outputs import CausalLMOutputWithCrossAttentions, BaseModelOutputWithPastAndCrossAttentions
from transformers.models.mbart.modeling_mbart import MBartPreTrainedModel, MBartDecoder, \
    MBartLearnedPositionalEmbedding
from surya.model.recognition.config import MBartMoEConfig
import torch
import math


class MBartExpertMLP(nn.Module):
    def __init__(self, config: MBartConfig, is_lg=False, is_xl=False):
        super().__init__()
        self.ffn_dim = config.d_expert
        if is_lg:
            self.ffn_dim = config.d_expert_lg
        if is_xl:
            self.ffn_dim = config.d_expert_xl
        self.hidden_dim = config.d_model

        self.w1 = nn.Linear(self.hidden_dim, self.ffn_dim, bias=False)
        self.w2 = nn.Linear(self.ffn_dim, self.hidden_dim, bias=False)
        self.w3 = nn.Linear(self.hidden_dim, self.ffn_dim, bias=False)
        self.dropout = nn.Dropout(config.activation_dropout)

        self.act_fn = ACT2FN[config.activation_function]

    def forward(self, hidden_states):
        current_hidden_states = self.act_fn(self.w1(hidden_states)) * self.w3(hidden_states)
        current_hidden_states = self.dropout(current_hidden_states)
        current_hidden_states = self.w2(current_hidden_states)
        return current_hidden_states


class MBartExpertLayer(nn.Module):
    # From mixtral, with modifications
    def __init__(self, config):
        super().__init__()
        self.dropout = nn.Dropout(config.activation_dropout)

        self.hidden_dim = config.d_model

        self.lg_lang_codes = sorted(config.lg_langs.values()) if hasattr(config, "lg_langs") else []
        self.xl_lang_codes = sorted(config.xl_langs.values()) if hasattr(config, "xl_langs") else []

        self.lang_codes = sorted(config.langs.values())
        self.num_experts = len(self.lang_codes)

        self.experts = nn.ModuleDict({str(lang): MBartExpertMLP(config, is_lg=(lang in self.lg_lang_codes), is_xl=(lang in self.xl_lang_codes)) for lang in self.lang_codes})

    def forward(self, hidden_states: torch.Tensor, langs: torch.LongTensor) -> torch.Tensor:
        batch_size, sequence_length, hidden_dim = hidden_states.shape

        final_hidden_states = torch.zeros(
            (batch_size, sequence_length, hidden_dim), dtype=hidden_states.dtype, device=hidden_states.device
        )

        # Weight experts based on how many languages in the input
        routing_weights = 1 / ((langs > 3).sum(axis=-1))
        # Set weights to 1 if zero experts activated
        routing_weights[torch.isinf(routing_weights)] = 1

        # Loop over all available experts in the model and perform the computation on each expert
        for expert_idx, expert_lang in enumerate(self.lang_codes):
            # Check which samples match with this expert
            lang_match = (langs == expert_lang).any(dim=-1)
            idx = torch.nonzero(lang_match, as_tuple=True)[0]

            if idx.shape[0] == 0:
                continue

            expert_layer = self.experts[str(expert_lang)]

            current_state = hidden_states[idx]
            current_hidden_states = expert_layer(current_state.view(-1, hidden_dim))
            current_hidden_states = self.dropout(current_hidden_states)
            current_hidden_states = current_hidden_states.view(-1, sequence_length, hidden_dim)

            # Weight by number of languages in the input
            selected_routing_weights = routing_weights[idx].view(-1, 1, 1)
            current_hidden_states *= selected_routing_weights

            final_hidden_states.index_add_(0, idx, current_hidden_states)

        return final_hidden_states


def repeat_kv(hidden_states: torch.Tensor, n_rep: int) -> torch.Tensor:
    """
    From llama
    This is the equivalent of torch.repeat_interleave(x, dim=1, repeats=n_rep). The hidden states go from (batch,
    num_key_value_heads, seqlen, head_dim) to (batch, num_attention_heads, seqlen, head_dim)
    """
    batch, num_key_value_heads, slen, head_dim = hidden_states.shape
    if n_rep == 1:
        return hidden_states
    hidden_states = hidden_states[:, :, None, :, :].expand(batch, num_key_value_heads, n_rep, slen, head_dim)
    return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)


class MBartGQAttention(nn.Module):
    def __init__(
        self,
        embed_dim: int,
        num_heads: int,
        num_kv_heads: int,
        dropout: float = 0.0,
        is_decoder: bool = False,
        bias: bool = True,
        is_causal: bool = False,
        config: Optional[MBartConfig] = None,
    ):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.num_kv_heads = num_kv_heads
        self.num_kv_groups = self.num_heads // self.num_kv_heads

        self.dropout = dropout
        self.head_dim = embed_dim // num_heads
        self.config = config
        self.scaling = self.head_dim**-0.5
        self.is_decoder = is_decoder
        self.is_causal = is_causal

        self.k_proj = nn.Linear(embed_dim, self.num_kv_heads * self.head_dim, bias=bias)
        self.v_proj = nn.Linear(embed_dim, self.num_kv_heads * self.head_dim, bias=bias)
        self.q_proj = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.out_proj = nn.Linear(embed_dim, embed_dim, bias=bias)

    def _shape(self, tensor: torch.Tensor, seq_len: int, bsz: int):
        return tensor.view(bsz, seq_len, self.num_heads, self.head_dim).transpose(1, 2).contiguous()

    def _shape_key_value(self, tensor: torch.Tensor, seq_len: int, bsz: int):
        return tensor.view(bsz, seq_len, self.num_kv_heads, self.head_dim).transpose(1, 2).contiguous()

    def forward(
        self,
        hidden_states: torch.Tensor,
        key_value_states: Optional[torch.Tensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        is_prefill: Optional[bool] = False,
        attention_mask: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        """Input shape: Batch x Time x Channel"""

        # if key_value_states are provided this layer is used as a cross-attention layer
        # for the decoder
        is_cross_attention = key_value_states is not None

        bsz, tgt_len, _ = hidden_states.size()

        # get query proj
        query_states = self.q_proj(hidden_states) * self.scaling
        # get key, value proj
        # `past_key_value[0].shape[2] == key_value_states.shape[1]`
        # is checking that the `sequence_length` of the `past_key_value` is the same as
        # the provided `key_value_states` to support prefix tuning
        if (
            is_cross_attention
            and not is_prefill
        ):
            # reuse k,v, cross_attentions
            key_states = past_key_value[0]
            value_states = past_key_value[1]
            past_key_value = (None, None)
        elif is_cross_attention:
            # cross_attentions
            key_states = self._shape_key_value(self.k_proj(key_value_states), -1, bsz)
            value_states = self._shape_key_value(self.v_proj(key_value_states), -1, bsz)
            past_key_value = (key_states, value_states)
        elif not is_prefill:
            # reuse k, v, self_attention
            key_states = self._shape_key_value(self.k_proj(hidden_states), -1, bsz)
            value_states = self._shape_key_value(self.v_proj(hidden_states), -1, bsz)
            key_states = torch.cat([past_key_value[0], key_states], dim=2)
            value_states = torch.cat([past_key_value[1], value_states], dim=2)
            past_key_value = (key_states[:, :, -tgt_len:], value_states[:, :, -tgt_len:])
        else:
            # self_attention
            key_states = self._shape_key_value(self.k_proj(hidden_states), -1, bsz)
            value_states = self._shape_key_value(self.v_proj(hidden_states), -1, bsz)
            past_key_value = (key_states[:, :, -tgt_len:], value_states[:, :, -tgt_len:])

        proj_shape = (bsz * self.num_heads, -1, self.head_dim)
        query_states = self._shape(query_states, tgt_len, bsz).view(*proj_shape)

        # Expand kv heads, then match query shape
        key_states = repeat_kv(key_states, self.num_kv_groups).reshape(*proj_shape)
        value_states = repeat_kv(value_states, self.num_kv_groups).reshape(*proj_shape)

        src_len = key_states.size(1)
        attn_weights = torch.bmm(query_states, key_states.transpose(1, 2))

        if attention_mask is not None:
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len) + attention_mask
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)

        attn_weights = nn.functional.softmax(attn_weights, dim=-1)

        attn_probs = nn.functional.dropout(attn_weights, p=self.dropout, training=self.training)
        attn_output = torch.bmm(attn_probs, value_states).view(bsz, self.num_heads, tgt_len, self.head_dim).transpose(1,2)

        # Use the `embed_dim` from the config (stored in the class) rather than `hidden_state` because `attn_output` can be
        # partitioned across GPUs when using tensor-parallelism.
        attn_output = attn_output.reshape(bsz, tgt_len, self.embed_dim)
        attn_output = self.out_proj(attn_output)

        return attn_output, past_key_value


class MBartMoEDecoderLayer(nn.Module):
    def __init__(self, config: MBartConfig, has_moe=False):
        super().__init__()
        self.embed_dim = config.d_model

        self.self_attn = MBartGQAttention(
            embed_dim=self.embed_dim,
            num_heads=config.decoder_attention_heads,
            num_kv_heads=config.kv_heads,
            dropout=config.attention_dropout,
            is_decoder=True,
            is_causal=True,
            config=config,
        )
        self.dropout = config.dropout
        self.activation_fn = ACT2FN[config.activation_function]
        self.activation_dropout = config.activation_dropout

        self.self_attn_layer_norm = nn.LayerNorm(self.embed_dim)
        self.encoder_attn = MBartGQAttention(
            self.embed_dim,
            config.decoder_attention_heads,
            num_kv_heads=config.kv_heads,
            dropout=config.attention_dropout,
            is_decoder=True,
            config=config,
        )
        self.encoder_attn_layer_norm = nn.LayerNorm(self.embed_dim)
        self.has_moe = has_moe
        if has_moe:
            self.moe = MBartExpertLayer(config)
        else:
            self.fc1 = nn.Linear(self.embed_dim, config.decoder_ffn_dim)
            self.fc2 = nn.Linear(config.decoder_ffn_dim, self.embed_dim)
        self.final_layer_norm = nn.LayerNorm(self.embed_dim)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        langs: Optional[torch.LongTensor] = None,
        kv_caches: Optional[List[torch.Tensor]] = None,
        is_prefill: Optional[bool] = False,
        encoder_hidden_states: Optional[torch.Tensor] = None,
        encoder_attention_mask: Optional[torch.Tensor] = None,
        use_cache: Optional[bool] = True,
    ) -> torch.Tensor:
        residual = hidden_states
        hidden_states = self.self_attn_layer_norm(hidden_states)

        # Self Attention
        # decoder uni-directional self-attention cached key/values tuple is at positions 1,2
        self_attn_past_key_value = kv_caches[0] if kv_caches is not None else None
        # add present self-attn cache to positions 1,2 of present_key_value tuple
        hidden_states, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            past_key_value=self_attn_past_key_value,
            is_prefill=is_prefill,
            attention_mask=attention_mask,
        )
        hidden_states = nn.functional.dropout(hidden_states, p=self.dropout, training=self.training)
        hidden_states = residual + hidden_states

        # Cross-Attention Block
        if encoder_hidden_states is not None:
            residual = hidden_states
            hidden_states = self.encoder_attn_layer_norm(hidden_states)

            # cross_attn cached key/values tuple is at positions 3,4 of present_key_value tuple
            cross_attn_past_key_value = kv_caches[1] if kv_caches is not None else None
            hidden_states, cross_attn_present_key_value = self.encoder_attn(
                hidden_states=hidden_states,
                key_value_states=encoder_hidden_states,
                is_prefill=is_prefill,
                attention_mask=encoder_attention_mask,
                past_key_value=cross_attn_past_key_value,
            )
            hidden_states = nn.functional.dropout(hidden_states, p=self.dropout, training=self.training)
            hidden_states = residual + hidden_states

            # add cross-attn to positions 3,4 of present_key_value tuple
            present_key_value = present_key_value + cross_attn_present_key_value

        # Fully Connected
        residual = hidden_states
        hidden_states = self.final_layer_norm(hidden_states)
        if self.has_moe:
            hidden_states = self.moe(hidden_states, langs)
        else:
            hidden_states = self.activation_fn(self.fc1(hidden_states))
            hidden_states = nn.functional.dropout(hidden_states, p=self.activation_dropout, training=self.training)
            hidden_states = self.fc2(hidden_states)
            hidden_states = nn.functional.dropout(hidden_states, p=self.dropout, training=self.training)

        hidden_states = residual + hidden_states

        outputs = (hidden_states,)

        if use_cache:
            outputs += (present_key_value,)

        return outputs

class MBartMoEDecoder(MBartDecoder):
    def __init__(self, config: MBartConfig, embed_tokens: Optional[nn.Embedding] = None):
        MBartPreTrainedModel.__init__(self, config)
        self.dropout = config.dropout
        self.layerdrop = config.decoder_layerdrop
        self.padding_idx = config.pad_token_id
        self.max_target_positions = config.max_position_embeddings
        self.embed_scale = math.sqrt(config.d_model) if config.scale_embedding else 1.0

        self.embed_tokens = nn.Embedding(config.vocab_size, config.d_model, self.padding_idx)

        if embed_tokens is not None:
            self.embed_tokens.weight = embed_tokens.weight

        self.embed_positions = MBartLearnedPositionalEmbedding(
            config.max_position_embeddings,
            config.d_model,
        )
        # Language-specific MoE goes at second and second-to-last layer
        self.layers = nn.ModuleList([MBartMoEDecoderLayer(config, has_moe=(i in config.moe_layers) and config.use_moe) for i in range(config.decoder_layers)])
        self._use_flash_attention_2 = config._attn_implementation == "flash_attention_2"
        self.layernorm_embedding = nn.LayerNorm(config.d_model)
        self.layer_norm = nn.LayerNorm(config.d_model)

        self.gradient_checkpointing = False
        # Initialize weights and apply final processing
        self.post_init()

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        kv_caches: Optional[List[torch.Tensor]] = None,
        past_token_count: Optional[int] = None,
        langs: Optional[torch.LongTensor] = None,
        encoder_hidden_states: Optional[torch.FloatTensor] = None,
        encoder_attention_mask: Optional[torch.LongTensor] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, BaseModelOutputWithPastAndCrossAttentions]:
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        input = input_ids
        input_shape = input.size()
        input_ids = input_ids.view(-1, input_shape[-1])

        # past_key_values_length
        past_key_values_length = past_token_count if kv_caches is not None else 0
        inputs_embeds = self.embed_tokens(input_ids) * self.embed_scale

        # 4d mask is passed through the layers
        attention_mask = _prepare_4d_causal_attention_mask(
            attention_mask, input_shape, inputs_embeds, past_key_values_length
        )

        # expand encoder attention mask
        if encoder_hidden_states is not None and encoder_attention_mask is not None:
            # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
            encoder_attention_mask = _prepare_4d_attention_mask(
                encoder_attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]
            )

        # embed positions
        positions = self.embed_positions(input, past_key_values_length)

        hidden_states = inputs_embeds + positions.to(inputs_embeds.device)
        hidden_states = self.layernorm_embedding(hidden_states)

        hidden_states = nn.functional.dropout(hidden_states, p=self.dropout, training=self.training)

        # decoder layers
        all_hidden_states = None
        all_self_attns = None
        all_cross_attentions = None
        next_decoder_cache = () if use_cache else None

        for idx, decoder_layer in enumerate(self.layers):
            is_prefill = past_token_count == 0
            kv_cache = [kv_caches[0][idx], kv_caches[1][idx]] if kv_caches is not None else None
            layer_outputs = decoder_layer(
                hidden_states,
                attention_mask=attention_mask,
                langs=langs,
                kv_caches=kv_cache,
                is_prefill=is_prefill,
                encoder_hidden_states=encoder_hidden_states,
                encoder_attention_mask=encoder_attention_mask,
                use_cache=use_cache,
            )
            hidden_states = layer_outputs[0]

            if use_cache:
                next_decoder_cache += (layer_outputs[1],)

        hidden_states = self.layer_norm(hidden_states)

        next_cache = next_decoder_cache if use_cache else None
        if not return_dict:
            return tuple(
                v
                for v in [hidden_states, next_cache, all_hidden_states, all_self_attns, all_cross_attentions]
                if v is not None
            )
        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            past_key_values=next_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
            cross_attentions=all_cross_attentions,
        )


class MBartMoEDecoderWrapper(MBartPreTrainedModel):
    """
    This wrapper class is a helper class to correctly load pretrained checkpoints when the causal language model is
    used in combination with the [`EncoderDecoderModel`] framework.
    """

    def __init__(self, config):
        super().__init__(config)
        self.decoder = MBartMoEDecoder(config)

    def forward(self, *args, **kwargs):
        return self.decoder(*args, **kwargs)


class MBartMoE(MBartForCausalLM):
    config_class = MBartMoEConfig
    _tied_weights_keys = ["lm_head.weight"]

    def __init__(self, config):
        config = copy.deepcopy(config)
        config.is_decoder = True
        config.is_encoder_decoder = False
        MBartPreTrainedModel.__init__(self, config)
        self.model = MBartMoEDecoderWrapper(config)

        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        kv_caches: Optional[List[torch.FloatTensor]] = None,
        past_token_count: Optional[int] = None,
        langs: Optional[torch.LongTensor] = None,
        encoder_hidden_states: Optional[torch.FloatTensor] = None,
        encoder_attention_mask: Optional[torch.FloatTensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        cross_attn_head_mask: Optional[torch.Tensor] = None,
        past_key_values: Optional[Tuple[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, CausalLMOutputWithCrossAttentions]:
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        outputs = self.model.decoder(
            input_ids=input_ids,
            attention_mask=attention_mask,
            kv_caches=kv_caches,
            past_token_count=past_token_count,
            langs=langs,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            return_dict=return_dict,
        )

        logits = self.lm_head(outputs[0])

        if not return_dict:
            output = (logits,) + outputs[1:]
            return output

        return CausalLMOutputWithCrossAttentions(
            loss=None,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            cross_attentions=outputs.cross_attentions,
        )

    def prepare_inputs_for_generation(
        self, input_ids, past_key_values=None, attention_mask=None, langs=None, use_cache=None, **kwargs
    ):
        # if model is used as a decoder in encoder-decoder model, the decoder attention mask is created on the fly
        if attention_mask is None:
            attention_mask = input_ids.new_ones(input_ids.shape)

        if past_key_values:
            past_length = past_key_values[0][0].shape[2]

            # Some generation methods already pass only the last input ID
            if input_ids.shape[1] > past_length:
                remove_prefix_length = past_length
            else:
                # Default to old behavior: keep only final ID
                remove_prefix_length = input_ids.shape[1] - 1

            input_ids = input_ids[:, remove_prefix_length:]
        # first step, decoder_cached_states are empty
        return {
            "input_ids": input_ids,  # encoder_outputs is defined. input_ids not needed
            "attention_mask": attention_mask,
            "past_key_values": past_key_values,
            "use_cache": use_cache,
            "langs": langs
        }

    def prune_moe_experts(self, keep_keys: List[int]):
        # Remove experts not specified in keep_keys
        str_keep_keys = [str(key) for key in keep_keys]
        for layer in self.model.decoder.layers:
            if not layer.has_moe:
                continue

            lang_keys = list(layer.moe.experts.keys())
            for lang in lang_keys:
                if lang not in str_keep_keys:
                    layer.moe.experts.pop(lang)
            layer.lang_codes = keep_keys