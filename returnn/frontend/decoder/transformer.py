"""
(Label-sync) Transformer decoder, including cross attention to encoder

References:

    (Original paper of course)
    https://pytorch.org/docs/stable/_modules/torch/nn/modules/transformer.html#Transformer
    https://github.com/pytorch-labs/gpt-fast
    https://github.com/karpathy/minGPT/blob/master/mingpt/model.py
    https://github.com/karpathy/nanoGPT/blob/master/model.py
    https://github.com/facebookresearch/fairseq/blob/main/fairseq/models/transformer/transformer_decoder.py
"""

from __future__ import annotations
from typing import Optional, Any, Union, Tuple, Dict, Callable, Sequence
import functools
import copy as _copy
from returnn.util.basic import NotSpecified
import returnn.frontend as rf
from returnn.tensor import Tensor, Dim, single_step_dim


class TransformerDecoder(rf.Module):
    """
    Represents Transformer decoder architecture
    """

    def __init__(
        self,
        encoder_dim: Dim,
        vocab_dim: Dim,
        model_dim: Dim = Dim(512, name="transformer-dec-default-model-dim"),
        *,
        num_layers: int,
        ff_dim: Dim = NotSpecified,
        ff_activation: Callable[[Tensor], Tensor] = rf.relu,
        dropout: float = 0.1,
        num_heads: int = 8,
        att_dropout: float = 0.1,
        decoder_layer: Optional[Union[TransformerDecoderLayer, rf.Module, type, Any]] = None,
        decoder_layer_opts: Optional[Dict[str, Any]] = None,
        share_embedding: bool = False,
    ):
        """
        :param encoder_dim:
        :param vocab_dim:
        :param model_dim: the output feature dimension
        :param num_layers: the number of encoder layers
        :param ff_dim: the dimension of feed-forward layers. 2048 originally, or 4 times out_dim
        :param ff_activation: activation function for feed-forward network
        :param dropout: the dropout value for the FF block
        :param num_heads: the number of attention heads
        :param att_dropout: attention dropout value
        :param decoder_layer: an instance of :class:`TransformerDecoderLayer` or similar
        :param decoder_layer_opts: options for the encoder layer
        :param share_embedding:
        """
        super().__init__()

        self.encoder_dim = encoder_dim
        self.vocab_dim = vocab_dim
        self.model_dim = model_dim

        # We could make this optional or configurable if we ever need to.
        # Or maybe you would just have another separate implementation of this module then...
        self.input_embedding = rf.Embedding(vocab_dim, model_dim)

        # This could also be configurable...
        self.pos_enc = functools.partial(
            rf.sinusoidal_positional_encoding, feat_dim=model_dim, dtype=self.input_embedding.weight.dtype
        )

        if not decoder_layer or isinstance(decoder_layer, type):
            decoder_layer_opts_ = dict(
                encoder_dim=encoder_dim,
                out_dim=model_dim,
                ff_dim=ff_dim,
                ff_activation=ff_activation,
                dropout=dropout,
                num_heads=num_heads,
                att_dropout=att_dropout,
            )
            if decoder_layer_opts:
                decoder_layer_opts_.update(decoder_layer_opts)
            if not decoder_layer:
                decoder_layer = TransformerDecoderLayer(**decoder_layer_opts_)
            elif isinstance(decoder_layer, type):
                decoder_layer = decoder_layer(**decoder_layer_opts_)
            else:
                raise TypeError(f"unexpected decoder_layer {decoder_layer!r}")

        self.layers = rf.Sequential(_copy.deepcopy(decoder_layer) for _ in range(num_layers))

        self.final_layer_norm = rf.LayerNorm(model_dim)

        self.logits = rf.Linear(model_dim, vocab_dim, with_bias=False)

        if share_embedding:
            self.logits.weight = self.input_embedding.weight

    def default_initial_state(self, *, batch_dims: Sequence[Dim]) -> rf.State:
        """default initial state"""
        state = rf.State({k: v.default_initial_state(batch_dims=batch_dims) for k, v in self.layers.items()})
        state.pos = rf.zeros((), dtype="int32", device="cpu")
        return state

    def transform_encoder(self, encoder: Tensor, *, axis: Dim) -> rf.State:
        """
        Transform encoder output.
        Note that the Transformer decoder usually expects that layer-norm was applied already on the encoder output.
        """
        return rf.State({k: v.transform_encoder(encoder, axis=axis) for k, v in self.layers.items()})

    def __call__(
        self,
        source: Tensor,
        *,
        spatial_dim: Dim,
        state: rf.State,
        encoder: rf.State,
        collected_outputs: Optional[Dict[str, Tensor]] = None,
    ) -> Tuple[Tensor, rf.State]:
        """
        forward, single step or whole sequence.

        :param source: labels
        :param spatial_dim: single_step_dim or spatial dim of source
        :param state: e.g. via :func:`default_initial_state`
        :param encoder: via :func:`transform_encoder`
        :param collected_outputs:
        :return: logits, new state
        """
        new_state = rf.State()

        decoded = self.input_embedding(source)
        decoded = decoded + self.pos_enc(spatial_dim=spatial_dim, offset=state.pos)

        new_state.pos = state.pos + (1 if spatial_dim == single_step_dim else spatial_dim.get_size_tensor())

        for layer_name, layer in self.layers.items():
            layer: TransformerDecoderLayer  # or similar
            decoded, new_state[layer_name] = layer(
                decoded, spatial_dim=spatial_dim, state=state[layer_name], encoder=encoder[layer_name]
            )
            if collected_outputs is not None:
                collected_outputs[layer_name] = decoded

        decoded = self.final_layer_norm(decoded)
        logits = self.logits(decoded)

        return logits, new_state


class TransformerDecoderLayer(rf.Module):
    """
    Represents a conformer block
    """

    def __init__(
        self,
        encoder_dim: Dim,
        out_dim: Dim = Dim(512, name="transformer-dec-default-out-dim"),
        *,
        ff_dim: Dim = NotSpecified,
        ff_activation: Callable[[Tensor], Tensor] = rf.relu,
        dropout: float = 0.1,
        num_heads: int = 8,
        self_att: Optional[Union[rf.CausalSelfAttention, rf.RelPosCausalSelfAttention, rf.Module, type, Any]] = None,
        self_att_opts: Optional[Dict[str, Any]] = None,
        att_dropout: float = 0.1,
    ):
        """
        :param encoder_dim:
        :param out_dim: the output feature dimension
        :param ff_dim: the dimension of feed-forward layers. 2048 originally, or 4 times out_dim
        :param ff_activation: activation function for feed-forward network
        :param dropout: the dropout value for the FF block
        :param num_heads: the number of attention heads
        :param self_att: the self-attention layer. RelPosSelfAttention originally and default
        :param self_att_opts: options for the self-attention layer, for :class:`nn.RelPosSelfAttention`
        :param att_dropout: attention dropout value
        """
        super().__init__()

        self.encoder_dim = encoder_dim
        self.dropout = dropout
        self.dropout_broadcast = rf.dropout_broadcast_default()
        self.out_dim = out_dim

        if ff_dim is None:
            ff_dim = 4 * out_dim
        self.ff = FeedForward(out_dim=out_dim, ff_dim=ff_dim, dropout=dropout, activation=ff_activation)
        self.ff_layer_norm = rf.LayerNorm(out_dim)

        if self_att is None or isinstance(self_att, type):
            self_att_opts_ = dict(
                in_dim=out_dim,
                proj_dim=out_dim,
                key_dim_total=out_dim,
                value_dim_total=out_dim,
                num_heads=num_heads,
                att_dropout=att_dropout,
            )
            if self_att_opts:
                self_att_opts_.update(self_att_opts)
            if self_att is None:
                self.self_att = rf.CausalSelfAttention(**self_att_opts_)
            else:
                self.self_att = self_att(**self_att_opts_)
        else:
            self.self_att = self_att
        self.self_att_layer_norm = rf.LayerNorm(out_dim)

        self.cross_att = rf.CrossAttention(
            encoder_dim=self.encoder_dim,
            query_in_dim=out_dim,
            proj_dim=out_dim,
            key_dim_total=out_dim,
            value_dim_total=out_dim,
            num_heads=num_heads,
            att_dropout=att_dropout,
        )
        self.cross_att_layer_norm = rf.LayerNorm(out_dim)

    def default_initial_state(self, *, batch_dims: Sequence[Dim]) -> rf.State:
        """default initial state"""
        return rf.State(self_att=self.self_att.default_initial_state(batch_dims=batch_dims))

    def transform_encoder(self, encoder: Tensor, *, axis: Dim) -> rf.State:
        """Transform the encoder output."""
        return rf.State(cross_att=self.cross_att.transform_encoder(encoder, axis=axis))

    def __call__(self, inp: Tensor, *, spatial_dim: Dim, state: rf.State, encoder: rf.State) -> Tuple[Tensor, rf.State]:
        """forward"""
        # (multi-head) self-attention (MHSA or simply SA)
        new_state = rf.State()
        x_sa_ln = self.self_att_layer_norm(inp)
        x_sa, new_state.self_att = self.self_att(x_sa_ln, axis=spatial_dim, state=state.self_att)
        x_sa = rf.dropout(x_sa, self.dropout, axis=self.dropout_broadcast and self.out_dim)
        x_sa_out = x_sa + inp

        # (multi-head) cross-attention (CA)
        x_ca_ln = self.cross_att_layer_norm(x_sa_out)
        x_ca = self.cross_att(x_ca_ln, encoder.cross_att)
        x_ca = rf.dropout(x_ca, self.dropout, axis=self.dropout_broadcast and self.out_dim)
        x_ca_out = x_ca + x_sa_out

        # feed-forward (FF)
        x_ff_ln = self.ff_layer_norm(x_ca_out)
        x_ff = self.ff(x_ff_ln)
        x_ff = rf.dropout(x_ff, self.dropout, axis=self.dropout_broadcast and self.out_dim)
        x_ff_out = x_ff + x_ca_out

        return x_ff_out, new_state


class FeedForward(rf.Module):
    """
    Conformer position-wise feedforward neural network layer
        FF -> Activation -> Dropout -> FF
    """

    def __init__(
        self,
        out_dim: Dim,
        *,
        ff_dim: Optional[Dim] = NotSpecified,
        dropout: float,
        activation: Callable[[Tensor], Tensor],
    ):
        """
        :param out_dim: output feature dimension
        :param ff_dim: dimension of the feed-forward layers
        :param dropout: dropout value
        :param activation: activation function
        """
        super().__init__()

        if ff_dim is NotSpecified:
            ff_dim = out_dim * 4

        self.out_dim = out_dim
        self.dropout = dropout
        self.dropout_broadcast = rf.dropout_broadcast_default()
        self.activation = activation

        self.linear_ff = rf.Linear(out_dim, ff_dim)
        self.linear_out = rf.Linear(ff_dim, out_dim)

    def __call__(self, inp: Tensor) -> Tensor:
        """forward"""
        x_ff1 = self.linear_ff(inp)
        x_act = self.activation(x_ff1)
        x_drop = rf.dropout(x_act, self.dropout, axis=self.dropout_broadcast and self.linear_ff.out_dim)
        x_ff2 = self.linear_out(x_drop)
        return x_ff2
