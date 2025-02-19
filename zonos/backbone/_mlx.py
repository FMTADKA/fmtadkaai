import mlx.core as mx
import mlx.nn as nn
from mlx.nn import functional as F

from zonos.config import BackboneConfig, InferenceParams

#pretty much copy of torch but mlxified

def precompute_freqs_cis(seq_len: int, n_elem: int, base: float = 10000) -> mx.array:
    freqs = 1.0 / (base ** (mx.arange(0, n_elem, 2)[: (n_elem // 2)].astype(mx.float32) / n_elem))
    t = mx.arange(seq_len, dtype=mx.float32)
    freqs = mx.outer(t, freqs)
    freqs_cis = mx.polar(mx.ones_like(freqs), freqs)
    cache = mx.stack([freqs_cis.real, freqs_cis.imag], axis=-1)
    return cache


def apply_rotary_emb(x: mx.array, freqs_cis: mx.array) -> mx.array:
    xshaped = x.astype(mx.float32).reshape(*x.shape[:-1], -1, 2)
    freqs_cis = freqs_cis.reshape(-1, xshaped.shape[1], 1, xshaped.shape[3], 2)
    x_out2 = mx.stack(
        [
            xshaped[..., 0] * freqs_cis[..., 0] - xshaped[..., 1] * freqs_cis[..., 1],
            xshaped[..., 1] * freqs_cis[..., 0] + xshaped[..., 0] * freqs_cis[..., 1],
        ],
        axis=-1,
    )

    x_out2 = x_out2.flatten(3)
    return x_out2.astype(x.dtype)


def _update_kv_cache(
    k: mx.array, v: mx.array, inference_params: InferenceParams, layer_idx: int
) -> mx.array:
    """k/v: (batch_size, seqlen, nheads, head_dim) or (batch_size, 1, nheads, head_dim)"""
    assert layer_idx in inference_params.key_value_memory_dict
    kv_cache, _ = inference_params.key_value_memory_dict[layer_idx]
    batch_start = inference_params.batch_size_offset
    batch_end = batch_start + k.shape[0]
    sequence_start = inference_params.seqlen_offset
    sequence_end = sequence_start + k.shape[1]
    assert batch_end <= kv_cache.shape[0]
    assert sequence_end <= kv_cache.shape[1]
    assert kv_cache is not None
    kv_cache[batch_start:batch_end, sequence_start:sequence_end, 0, ...] = k
    kv_cache[batch_start:batch_end, sequence_start:sequence_end, 1, ...] = v
    return kv_cache[batch_start:batch_end, :sequence_end, ...]


class MLXZonosBackbone(nn.Module):
    supported_architectures = ["transformer"]
    freqs_cis: mx.array

    def __init__(self, config: BackboneConfig):
        assert not config.ssm_cfg, "This backbone implementation only supports the Transformer model."
        super().__init__()
        self.config = config

        self.layers = nn.ModuleList(TransformerBlock(config, i) for i in range(config.n_layer))
        self.norm_f = nn.LayerNorm(config.d_model, eps=config.norm_epsilon)

    def allocate_inference_cache(self, batch_size: int, max_seqlen: int, dtype: mx.Dtype = mx.bfloat16):
        # TODO: This function should be pure
        head_dim = self.config.d_model // self.config.attn_cfg["num_heads"]
        self.freqs_cis = precompute_freqs_cis(16384, head_dim)
        return {
            i: layer.allocate_inference_cache(batch_size, max_seqlen, dtype=dtype)
            for i, layer in enumerate(self.layers)
        }

    def forward(self, hidden_states: mx.array, inference_params: InferenceParams) -> mx.array:
        input_pos = mx.arange(0, hidden_states.shape[1], device=hidden_states.device)
        input_pos = input_pos + inference_params.lengths_per_sample.unsqueeze(-1)

        freqs_cis = self.freqs_cis[input_pos].expand(hidden_states.shape[0], -1, -1, -1)
        for i, layer in enumerate(self.layers):
            hidden_states = layer(hidden_states, inference_params, freqs_cis)
        return self.norm_f(hidden_states)


class TransformerBlock(nn.Module):
    def __init__(self, config: BackboneConfig, layer_idx: int) -> None:
        super().__init__()
        self.config = config

        self.norm = nn.LayerNorm(config.d_model, eps=config.norm_epsilon)
        self.mixer = Attention(config, layer_idx)
        self.norm2 = nn.LayerNorm(config.d_model, eps=config.norm_epsilon)
        self.mlp = FeedForward(config)

        self.num_heads_kv = config.attn_cfg["num_heads_kv"]
        self.head_dim = config.d_model // config.attn_cfg["num_heads"]

    def allocate_inference_cache(self, batch_size: int, max_seqlen: int, dtype: mx.Dtype = mx.bfloat16):
        return mx.empty(batch_size, max_seqlen, 2, self.num_heads_kv, self.head_dim, dtype=dtype), None

    def forward(self, x: mx.array, inference_params: InferenceParams, freqs_cis: mx.array) -> mx.array:
        x = x + self.mixer(self.norm(x), inference_params, freqs_cis)
        x = x + self.mlp(self.norm2(x))
        return x


class Attention(nn.Module):
    def __init__(self, config: BackboneConfig, layer_idx: int):
        super().__init__()
        self.num_heads = config.attn_cfg["num_heads"]
        self.num_heads_kv = config.attn_cfg["num_heads_kv"]
        self.head_dim = config.d_model // self.num_heads
        self.layer_idx = layer_idx

        total_head_dim = (self.num_heads + 2 * self.num_heads_kv) * self.head_dim
        self.in_proj = nn.Linear(config.d_model, total_head_dim, bias=False)
        self.out_proj = nn.Linear(self.num_heads * self.head_dim, config.d_model, bias=False)

    def forward(self, x: mx.array, inference_params: InferenceParams, freqs_cis: mx.array) -> mx.array:
        batch_size, seqlen, _ = x.shape

        q_size = self.num_heads * self.head_dim
        kv_size = self.num_heads_kv * self.head_dim
        q, k, v = self.in_proj(x).split([q_size, kv_size, kv_size], axis=-1)

        q = q.reshape(batch_size, seqlen, self.num_heads, self.head_dim)
        k = k.reshape(batch_size, seqlen, self.num_heads_kv, self.head_dim)
        v = v.reshape(batch_size, seqlen, self.num_heads_kv, self.head_dim)

        q = apply_rotary_emb(q, freqs_cis)
        k = apply_rotary_emb(k, freqs_cis)

        kv = _update_kv_cache(k, v, inference_params, self.layer_idx)
        k, v = kv.unbind(axis=-3)

        q, k, v = map(lambda x: x.transpose(1, 2), (q, k, v))

        y = F.scaled_dot_product_attention(q, k, v, is_causal=seqlen > 1, enable_gqa=True)

        y = y.transpose(1, 2).reshape(batch_size, seqlen, q_size)

        y = self.out_proj(y)
        return y


class FeedForward(nn.Module):
    def __init__(self, config: BackboneConfig) -> None:
        super().__init__()
        self.fc1 = nn.Linear(config.d_model, 2 * config.attn_mlp_d_intermediate, bias=False)
        self.fc2 = nn.Linear(config.attn_mlp_d_intermediate, config.d_model, bias=False)

    def forward(self, x: mx.array) -> mx.array:
        y, gate = self.fc1(x).split(2, axis=-1)
        return self.fc2(y * F.silu(gate))