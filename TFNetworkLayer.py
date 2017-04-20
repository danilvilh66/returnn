
from __future__ import print_function

import tensorflow as tf
from TFUtil import Data, OutputWithActivation, reuse_name_scope, var_creation_scope


class LayerBase(object):
  layer_class = None
  recurrent = False

  def __init__(self, name, network, output=None, n_out=None, out_type=None, sources=(),
               target=None, loss=None, L2=None, is_output_layer=None,
               batch_norm=False,
               spatial_smoothing=0.0,
               trainable=True):
    """
    :param str name:
    :param TFNetwork.TFNetwork network:
    :param Data output:
    :param None|int n_out: output dim
    :param dict[str] out_type: kwargs for Data class. more explicit than n_out.
    :param list[LayerBase] sources: via self.transform_config_dict()
    :param str|None target: if some loss is set, this is the target data-key, i.e. network.extern_data.get_data(target)
      alternatively, this also can be a layer name.
    :param Loss|None loss: via self.transform_config_dict()
    :param float|None L2: for constraints
    :param bool|None is_output_layer:
    :param bool|dict batch_norm:
    :param bool trainable: whether the parameters of this layer will be trained
    """
    self.name = name
    self.network = network
    if loss and not target:
      target = self.network.extern_data.default_target
    self.target = target
    self.loss = loss
    if self.loss and self.loss.recurrent:
      self.recurrent = True
    if output:
      self.output = output
      if n_out:
        assert self.output.dim == n_out
      if out_type:
        if "shape" in out_type:
          assert self.output.shape == out_type["shape"]
        if "dim" in out_type:
          assert self.output.dim == out_type["dim"]
    else:
      self.output = self.get_out_data_from_opts(
        out_type=out_type, n_out=n_out, network=network, name=name, target=target, sources=sources, loss=loss)
    self.output_before_activation = None  # type: None|OutputWithActivation
    self.sources = sources
    self.params = {}  # type: dict[str,tf.Variable]
    self.L2 = L2
    self._is_output_layer = is_output_layer
    self.use_batch_norm = batch_norm
    self.spatial_smoothing = spatial_smoothing
    self.trainable = trainable
    # Stats will be collected by the engine.
    self.stats = {}  # type: dict[str,tf.Tensor]

  def post_init(self):
    """
    This gets called right after self.__init__().
    """
    if self.use_batch_norm:
      opts = {}
      if isinstance(self.use_batch_norm, dict):
        opts = self.use_batch_norm
      self.output.placeholder = self.batch_norm(self.output, **opts)

  @classmethod
  def get_out_data_from_opts(cls, **kwargs):
    """
    Gets a Data template (i.e. shape etc is set but not the placeholder) for our __init__ args.
    The purpose of having this as a separate classmethod is to be able to infer the shape information
    without having to construct the layer.
    This function should not create any nodes in the computation graph.

    :param dict[str] kwargs: all the same kwargs as for self.__init__()
    :return: Data template (placeholder not set)
    :rtype: Data
    """
    return cls._base_get_out_data_from_opts(**kwargs)

  @classmethod
  def _base_get_out_data_from_opts(cls, network, name, out_type=None, n_out=None, target=None, sources=(), loss=None,
                                   **kwargs):
    """
    Called via BaseLayer.get_out_data_from_opts().

    :param TFNetwork.TFNetwork network:
    :param str name:
    :param dict[str]|None out_type:
    :param int|None n_out:
    :param str|None target:
    :param list[LayerBase] sources:
    :param Loss|None loss:
    :param kwargs: remaining kwargs of self.__init__(), ignored here
    :return: Data template (placeholder not set)
    :rtype: Data
    """
    if loss and not target:
      target = network.extern_data.default_target
    if out_type is None and n_out is None and target:
      n_out = cls._static_get_target_value(target=target, network=network, mark_data_key_as_used=False).dim
      if loss:
        n_out = loss.get_auto_output_layer_dim(n_out)
    if out_type is None:
      assert n_out
      out_type = {"dim": n_out}
    out_type = out_type.copy()
    out_type.setdefault("name", "%s_output" % name)
    if sources and not sources[0].output.sparse:
      out_type.setdefault("dtype", sources[0].output.dtype)
    if n_out is not None:
      out_type.setdefault("dim", n_out)
      assert out_type["dim"] == n_out
    if sources:
      out_type.setdefault("shape", sources[0].output.shape[:-1] + (out_type.get("dim", sources[0].output.dim),))
    # You are supposed to set self.output.{batch_dim_axis,time_dim_axis} explicitly,
    # as well as check the inputs if they are as you would suggest.
    # However, a good default is often to use the same as the input.
    if sources and "batch_dim_axis" not in out_type:
      out_type.setdefault("batch_dim_axis", sources[0].output.batch_dim_axis)
      out_type.setdefault("time_dim_axis", sources[0].output.time_dim_axis)
    output = Data(**out_type)
    # You are supposed to set self.output.placeholder to the value which you want to return by the layer.
    # Normally you are also supposed to set self.output.size_placeholder explicitly, just like self.output.placeholder.
    # However, in many cases, this will just be {0: time-lengths} and the same as from the input.
    # We check for this case and preset it by that if possible.
    # If you want to have it different in your layer, just overwrite it.
    if sources and sources[0].output.matches_var_dim_pattern(output):
      output.size_placeholder = sources[0].output.size_placeholder.copy()
    elif target:
      # TODO: In training, this is ok. Maybe as well as for eval but not clear.
      # In forward, mark_data_key_as_used=False should be used and anyway that target value is not available.
      output.size_placeholder = cls._static_get_target_value(
        target=target, network=network, mark_data_key_as_used=network.train_flag is not False).size_placeholder.copy()
    return output

  def __repr__(self):
    return "%s{class=%s, out_type=%s}" % (
      self.name, self.layer_class, self.output.get_description(with_name=False))

  @classmethod
  def cls_get_tf_scope_name(cls, name):
    """
    :param str name: layer name
    :return: scope name, might be just name
    """
    return name.replace(":", "__")

  @classmethod
  def transform_config_dict(cls, d, network, get_layer):
    """
    :param dict[str] d: will modify inplace
    :param TFNetwork.TFNetwork network:
    :param ((str) -> LayerBase) get_layer: function to get or construct another layer

    Will modify `d` such that it becomes the kwargs for `self.__init__()`.
    Mostly leaves `d` as-is.
    This is used by TFNetwork.construct_from_dict().
    """
    src_names = d.pop("from", ["data"])
    if not isinstance(src_names, (list, tuple)):
      src_names = [src_names]
    d["sources"] = [
      get_layer(src_name)
      for src_name in src_names
      if not src_name == "none"]
    if d.get("loss"):
      loss_class = get_loss_class(d["loss"])
      d["loss"] = loss_class(**d.pop("loss_opts", {}))
    if d.get("target"):
      # Not resolving this in the dict, but call get_layer to make it available.
      assert isinstance(d["target"], str)
      if d["target"].startswith("layer:"):
        get_layer(d["target"][len("layer:"):])

  @property
  def tf_scope_name(self):
    return self.cls_get_tf_scope_name(name=self.name)

  def is_output_layer(self):
    """
    Some code differs between an output layer and other layers.
    It is a bit arbitrary what we define as output layer.
    :rtype: bool
    """
    if self._is_output_layer is not None:
      return self._is_output_layer
    if self.target:
      return True
    if self.name == "output":
      return True
    return False

  def add_param(self, param):
    """
    :param tf.Variable param:
    :return: param
    :rtype tf.Variable
    """
    assert param.name
    self.params[param.name] = param
    return param

  def set_param_values_by_dict(self, values_dict, session):
    """
    :param dict[str,numpy.ndarray] values_dict:
    :param tf.Session session:
    """
    for param_name, values in values_dict.items():
      param = self.params[param_name]
      assert isinstance(param, tf.Variable)
      shape = param.get_shape()
      assert isinstance(shape, tf.TensorShape)
      assert shape.is_fully_defined()
      assert tuple(shape.as_list()) == values.shape
      self.network.get_var_assigner(param).assign(values, session=session)

  def get_param_values_dict(self, session):
    """
    :param tf.Session session:
    :return: dict name -> values
    :rtype: dict[str,numpy.ndarray]
    """
    d = {}
    for param_name, param in self.params.items():
      d[param_name] = param.eval(session)
    return d

  @staticmethod
  def _static_get_target_value(target, network, mark_data_key_as_used=True):
    """
    :param str target:
    :param TFNetwork.TFNetwork network:
    :param bool mark_data_key_as_used: forwarded self.network.get_extern_data()
    :rtype: Data | None
    """
    if not target or target == "none":
      return None
    if target.startswith("layer:"):
      return network.layers[target[len("layer:"):]].output
    assert network.extern_data.has_data(target), "target %r unknown" % target
    return network.get_extern_data(target, mark_data_key_as_used=mark_data_key_as_used)

  def _get_target_value(self, mark_data_key_as_used=True):
    """
    :param bool mark_data_key_as_used: forwarded self.network.get_extern_data()
    :rtype: Data | None
    """
    return self._static_get_target_value(
      target=self.target, network=self.network, mark_data_key_as_used=mark_data_key_as_used)

  def _init_loss(self):
    if self.loss.output is self.output:
      return
    self.loss.init(
      output=self.output,
      output_with_activation=self.output_before_activation,
      target=self._get_target_value())

  def get_loss_value(self):
    """
    :return: the loss, a scalar value, or None if not set
    :rtype: tf.Tensor | None
    """
    if not self.loss:
      return None
    self._init_loss()
    with tf.name_scope("loss"):
      return self.loss.get_value()

  def get_error_value(self):
    """
    :return: usually the frame error rate, or None if not defined
    :rtype: tf.Tensor | None
    """
    if not self.loss:
      return None
    self._init_loss()
    with tf.name_scope("error"):
      return self.loss.get_error()

  def get_params_l2_norm(self):
    return 2 * sum([tf.nn.l2_loss(param) for (name, param) in sorted(self.params.items())])

  def get_output_spatial_smoothing_energy(self):
    from TFUtil import spatial_smoothing_energy, flatten_with_seq_len_mask
    energy = spatial_smoothing_energy(self.output.placeholder, dim=self.output.dim)  # (batch,time)
    assert self.output.have_tim_axis()
    energy = flatten_with_seq_len_mask(
      energy,
      seq_lens=self.output.size_placeholder[self.output.time_dim_axis_excluding_batch],
      time_major=self.output.is_time_major)  # (time')
    energy = tf.reduce_sum(energy)
    return energy

  def get_constraints_value(self):
    c = 0
    if self.L2:
      c += self.L2 * self.get_params_l2_norm()
    if self.spatial_smoothing:
      c += self.spatial_smoothing * self.get_output_spatial_smoothing_energy()
    if c is 0:
      return None
    return c

  def batch_norm(self, data,
                 use_shift=True, use_std=True, use_sample=0.0, force_sample=False,
                 momentum=0.99, epsilon=1e-3,
                 sample_mean=None, sample_variance=None,
                 gamma=None, beta=None):
    """
    :param Data data:
    :param bool use_shift:
    :param bool use_std:
    :param float use_sample: defaults to 0.0 which is used in training
    :param bool force_sample: even in eval, use the use_sample factor
    :param float momentum: for the running average of sample_mean and sample_std
    :param float epsilon:
    :param tf.Tensor sample_mean:
    :param tf.Tensor sample_variance:
    :param tf.Tensor gamma:
    :param tf.Tensor beta:
    :rtype: tf.Tensor

    http://arxiv.org/abs/1502.03167

    Also see:
      tf.nn.batch_normalization()
      https://github.com/deepmind/sonnet/blob/master/sonnet/python/modules/batch_norm.py
    """
    with tf.name_scope("batch_norm"):
      x = data.get_placeholder_flattened(keep_dims=True)  # shape (time',...)
      mean, variance = tf.nn.moments(x, axes=[0], keep_dims=True)
      if sample_mean is None:
        with var_creation_scope():
          sample_mean = self.add_param(tf.Variable(
            initial_value=tf.zeros(data.non_dynamic_batch_shape),
            name="%s_%s_mean" % (self.name, data.name),
            trainable=False))
        # Use exponential moving average of batch mean.
        # Note: We could also use cumulative moving average. Our Theano implementation does that for inference.
        sample_mean = tf.assign_add(sample_mean, (mean - sample_mean) * momentum)
      if sample_variance is None:
        # Note: Our Theano implementation does not use a moving average for this.
        with var_creation_scope():
          sample_variance = self.add_param(tf.Variable(
            initial_value=tf.ones(data.non_dynamic_batch_shape),
            name="%s_%s_variance" % (self.name, data.name),
            trainable=False))
        sample_variance = tf.assign_add(sample_variance, (variance - sample_variance) * momentum)
      # If train or if force_sample, use default use_sample=0.0, otherwise use_sample=1.0.
      use_sample = 1.0 + tf.cast(tf.logical_or(self.network.train_flag, force_sample), tf.float32) * (use_sample - 1.0)
      mean = (1. - use_sample) * mean + use_sample * sample_mean
      variance = (1. - use_sample) * variance + use_sample * sample_variance
      bn = (data.placeholder - mean) * tf.rsqrt(variance + epsilon)
      if use_std:
        if gamma is None:
          with var_creation_scope():
            gamma = self.add_param(tf.Variable(
              initial_value=tf.ones(data.non_dynamic_batch_shape),
              name="%s_%s_gamma" % (self.name, data.name),
              trainable=True))
        bn *= gamma
      if use_shift:
        if beta is None:
          with var_creation_scope():
            beta = self.add_param(tf.Variable(
              initial_value=tf.zeros(data.non_dynamic_batch_shape),
              name="%s_%s_beta" % (self.name, data.name),
              trainable=True))
        bn += beta
      return bn

  def get_hidden_state(self):
    """
    If this is a recurrent layer, this would return the hidden state.
    This is used e.g. for the RnnCellLayer class.
    :rtype: tf.Tensor | list[tf.Tensor] | None
    :return: optional tensor(s) with shape (time, batch, dim)
    """
    return None

  def get_last_hidden_state(self):
    """
    If this is a recurrent layer, this would return the last hidden state.
    If not, as a fallback, we recursively check our sources.
    :rtype: tf.Tensor | None
    :return: optional tensor with shape (batch, dim)
    """
    # This is the generic fallback code.
    hidden_states = []
    for s in self.sources:
      h = s.get_last_hidden_state()
      if h is not None:
        assert h.get_shape().ndims == 2
        hidden_states += [h]
    if not hidden_states:
      return None
    if len(hidden_states) == 1:
      return hidden_states[0]
    return tf.concat(hidden_states, axis=1, name="concat_hidden_states")


class SourceLayer(LayerBase):
  layer_class = "source"

  def __init__(self, network, data_key=None, sources=(), **kwargs):
    """
    :param TFNetwork.TFNetwork network:
    :param str|None data_key:
    :param tuple sources:
    """
    if data_key is None:
      data_key = network.extern_data.default_input
    assert not sources, "source layer does not expect sources"
    data = network.get_extern_data(data_key, mark_data_key_as_used=True)
    super(SourceLayer, self).__init__(network=network, **kwargs)
    self.output = data

  @classmethod
  def get_out_data_from_opts(cls, network, data_key=None, **kwargs):
    if data_key is None:
      data_key = network.extern_data.default_input
    return network.get_extern_data(data_key, mark_data_key_as_used=False)


def concat_sources(src_layers):
  """
  :param list[LayerBase] src_layers:
  :return: data with placeholders set
  :rtype: Data
  """
  assert src_layers, "need source layers"
  if len(src_layers) == 1:
    return src_layers[0].output
  network = src_layers[0].network
  if (tuple(src_layers), 0.0) in network.concat_sources_dropout_cache:
    return network.concat_sources_dropout_cache[(tuple(src_layers), 0.0)].copy()
  data = get_concat_sources_data_template(src_layers)
  prefix_shape = data.shape[:-1]  # without batch-dim
  for layer in src_layers:
    assert not layer.output.sparse, "sparse concat not supported"
    assert layer.output.dtype == data.dtype, "incompatible dtype with layer %r" % layer
    assert layer.output.time_dim_axis_excluding_batch == data.time_dim_axis_excluding_batch
    shape = layer.output.shape
    assert layer.output.placeholder.get_shape().ndims == len(shape) + 1  # with batch-dim
    assert shape, "source must not be a scalar of layer %r" % layer
    assert shape[:-1] == prefix_shape, "incompatible concat with layer %r" % layer
    assert shape[-1], "source last-dim must be specified of layer %r" % layer
  data.placeholder = tf.concat(
    axis=len(prefix_shape) + 1,  # one more because this is with batch-dim
    values=[layer.output.get_placeholder_with_specific_batch_dim_axis(data.batch_dim_axis) for layer in src_layers])
  data.size_placeholder = src_layers[0].output.size_placeholder.copy()
  network.concat_sources_dropout_cache[(tuple(src_layers), 0.0)] = data.copy()
  return data


def get_concat_sources_data_template(src_layers):
  """
  :param list[LayerBase] src_layers:
  :return: data with no placeholders set
  :rtype: Data
  """
  assert src_layers, "need source layers"
  dim = 0
  for layer in src_layers:
    shape = layer.output.shape
    assert shape[-1], "source last-dim must be specified of layer %r" % layer
    dim += shape[-1]
  data = Data(
    name="concat_sources",
    shape=src_layers[0].output.shape[:-1] + (dim,),
    dim=dim,
    sparse=False,
    batch_dim_axis=src_layers[0].output.batch_dim_axis,
    time_dim_axis=src_layers[0].output.time_dim_axis,
    dtype=src_layers[0].output.dtype)
  return data


def concat_sources_with_opt_dropout(src_layers, dropout=0):
  """
  :param list[LayerBase] src_layers:
  :param float dropout: will be applied if train_flag is set
  :return: data with placeholders set
  :rtype: Data
  """
  assert src_layers, "need source layers"
  data = concat_sources(src_layers)
  network = src_layers[0].network
  if network.train_flag is False:
    # If we know that we are not training, we always disable dropout.
    dropout = 0
  if not dropout:
    return data
  if (tuple(src_layers), float(dropout)) in network.concat_sources_dropout_cache:
    return network.concat_sources_dropout_cache[(tuple(src_layers), float(dropout))].copy()
  assert 0.0 < dropout < 1.0
  fn_train = lambda: tf.nn.dropout(
      data.placeholder,
      keep_prob=1 - dropout,
      # noise_shape is like old behavior for now:
      # all dynamic dimensions (batch,time) will use the same dropout-mask broadcasted.
      noise_shape=data.non_dynamic_batch_shape,
      seed=network.random.randint(2 ** 31))
  fn_eval = lambda: data.placeholder
  data.placeholder = network.cond_on_train(fn_train, fn_eval)
  network.concat_sources_dropout_cache[(tuple(src_layers), float(dropout))] = data.copy()
  return data


class _ConcatInputLayer(LayerBase):
  def __init__(self, dropout=0, mask=None, **kwargs):
    """
    :param float dropout: 0.0 means to apply no dropout. dropout will only be applied during training
    :param str|None mask: "dropout" or "unity" or None. this is obsolete and only here for historical reasons
    """
    super(_ConcatInputLayer, self).__init__(**kwargs)
    assert mask in ['dropout', 'unity', None], "invalid mask: %r" % mask
    if mask == "unity":
      assert not dropout
    elif mask == "dropout":
      assert dropout > 0
    self.input_data = None
    if self.sources:
      self.input_data = concat_sources_with_opt_dropout(self.sources, dropout=dropout)


class CopyLayer(_ConcatInputLayer):
  """
  This layer does nothing, it copies its input.
  If multiple sources are provided, they are concatenated in the feature-dim.
  """

  layer_class = "copy"

  def __init__(self, **kwargs):
    super(CopyLayer, self).__init__(**kwargs)
    self.output = self.input_data

  @classmethod
  def get_out_data_from_opts(cls, sources=(), **kwargs):
    return get_concat_sources_data_template(sources)


class ActivationLayer(CopyLayer):
  """
  This layer just applies an activation function.
  """

  layer_class = "activation"

  def __init__(self, activation, **kwargs):
    """
    :param str activation: e.g. "relu", "tanh", etc
    """
    super(ActivationLayer, self).__init__(**kwargs)
    x = self.input_data.placeholder
    if activation:
      from TFUtil import get_activation_function
      act_func = get_activation_function(activation)
      self.output_before_activation = OutputWithActivation(x, act_func=act_func)
    else:
      self.output_before_activation = OutputWithActivation(x)
    self.output.placeholder = self.output_before_activation.y


class BatchNormLayer(CopyLayer):
  layer_class = "batch_norm"

  def __init__(self, **kwargs):
    kwargs = kwargs.copy()
    import inspect
    batch_norm_kwargs = inspect.getargspec(self.batch_norm).args[1:]  # first is self, ignore
    batch_norm_opts = {key: kwargs.pop(key)
                       for key in batch_norm_kwargs
                       if key in kwargs}
    super(BatchNormLayer, self).__init__(use_batch_norm=batch_norm_opts or True, **kwargs)


class SliceLayer(_ConcatInputLayer):
  layer_class = "slice"

  def __init__(self, axis=None, axis_kind=None,
               slice_start=None, slice_end=None, slice_step=None,
               **kwargs):
    """
    :param int|None axis:
    :param str|None axis_kind: "T" for time, "B" for batch, "F" for feature
    :param int|None slice_start:
    :param int|None slice_end:
    :param int|None slice_step:
    :param int|None n_out:
    """
    super(SliceLayer, self).__init__( **kwargs)
    axis = self._get_axis(axis=axis, axis_kind=axis_kind, input_data=self.input_data)
    dim_slice = slice(slice_start, slice_end, slice_step)
    slices = [slice(None, None)] * axis + [dim_slice]
    axis_wo_batch = self.input_data.get_batch_axis_excluding_batch(axis)
    self.output.size_placeholder = self.input_data.size_placeholder
    if axis == self.input_data.time_dim_axis:
      if slice_start:
        assert slice_start > 0
        self.output.size_placeholder[self.input_data.time_dim_axis_excluding_batch] = \
          tf.maximum(0, self.output.size_placeholder[self.input_data.time_dim_axis_excluding_batch] - slice_start)
      if slice_end:
        assert slice_end > 0
        self.output.size_placeholder[self.input_data.time_dim_axis_excluding_batch] = \
          tf.minimum(
            tf.shape(self.input_data.placeholder)[self.input_data.time_dim_axis] - slice_end,
            self.output.size_placeholder[self.input_data.time_dim_axis_excluding_batch])
      if slice_step:
        self.output.size_placeholder[self.input_data.time_dim_axis_excluding_batch] //= slice_step
    elif axis_wo_batch is not None:
      assert axis_wo_batch not in self.output.size_placeholder
    self.output.placeholder = self.input_data.placeholder[slices]

  @classmethod
  def _get_axis(cls, axis, axis_kind, input_data):
    """
    :param int|None axis:
    :param str|None axis_kind: "T" for time, "B" for batch, "F" for feature
    :param Data input_data:
    :return: axis
    :rtype: int
    """
    if axis is not None:
      assert not axis_kind
      assert 0 <= axis < len(input_data.batch_shape)
    else:
      assert axis_kind
      axis_kind = axis_kind.upper()
      if axis_kind == "T":
        assert input_data.time_dim_axis is not None
        axis = input_data.time_dim_axis
      elif axis_kind == "B":
        assert input_data.batch_dim_axis is not None
        axis = input_data.batch_dim_axis
      elif axis_kind == "F":
        axes = input_data.get_axes(exclude_time=True, exclude_batch=True)
        assert len(axes) == 1
        axis = axes[0]
    return axis

  @classmethod
  def get_out_data_from_opts(
        cls, axis=None, axis_kind=None, sources=(),
        slice_start=None, slice_end=None, slice_step=None, **kwargs):
    input_data = get_concat_sources_data_template(sources)
    axis = cls._get_axis(axis=axis, axis_kind=axis_kind, input_data=input_data)
    out_type = input_data.get_kwargs()
    axis_wo_batch = input_data.get_batch_axis_excluding_batch(axis)
    dim_slice = slice(slice_start, slice_end, slice_step)
    if axis_wo_batch is not None:
      out_type["shape"] = list(out_type["shape"])
      if out_type["shape"][axis_wo_batch] is not None:
        out_type["shape"][axis_wo_batch] = len(range(out_type["shape"][axis_wo_batch])[dim_slice])
      if axis_wo_batch == len(out_type["shape"]) - 1 and not out_type["sparse"]:
        out_type["dim"] = out_type["shape"][axis_wo_batch]
    return Data(**out_type)


class LinearLayer(_ConcatInputLayer):
  layer_class = "linear"

  def __init__(self, activation, with_bias=True, **kwargs):
    super(LinearLayer, self).__init__(**kwargs)

    self.activation = activation
    self.with_bias = with_bias

    input_data = self.input_data
    n_in = input_data.dim
    n_out = self.output.dim
    assert n_in and n_out, "%r and %r" % (input_data, self.output)

    with var_creation_scope():
      W = self.add_param(tf.Variable(
        name="W",
        initial_value=tf.contrib.layers.xavier_initializer(seed=self.network.random.randint(2**31))(
          shape=(n_in, n_out))))

      if self.with_bias:
        b = self.add_param(tf.Variable(
          name="b",
          initial_value=tf.constant_initializer(value=0, dtype=tf.float32)(
            shape=(n_out,))))
      else:
        b = None

    with tf.name_scope("linear"):
      from TFUtil import dot
      x = input_data.placeholder
      ndim = x.get_shape().ndims

      if self.input_data.sparse:
        if x.dtype in [tf.uint8, tf.int8, tf.uint16, tf.int16]:
          x = tf.cast(x, tf.int32)
        x = tf.nn.embedding_lookup(W, x)
        ndim += 1
      else:
        x = dot(x, W)
      assert x.get_shape().ndims == ndim

      if self.with_bias:
        x = tf.add(x, b, name="add_bias")
        assert x.get_shape().ndims == ndim

    if self.activation:
      from TFUtil import get_activation_function
      act_func = get_activation_function(self.activation)
      self.output_before_activation = OutputWithActivation(x, act_func=act_func)
    else:
      self.output_before_activation = OutputWithActivation(x)
    x = self.output_before_activation.y

    assert self.output.batch_dim_axis == self.input_data.batch_dim_axis
    assert self.output.time_dim_axis == self.input_data.time_dim_axis
    self.output.placeholder = x


class SoftmaxLayer(LinearLayer):
  layer_class = "softmax"

  def __init__(self, activation="softmax", **kwargs):
    super(SoftmaxLayer, self).__init__(activation=activation, **kwargs)


class ConvLayer(_ConcatInputLayer):
  """
  A generic convolution layer which supports 1D, 2D and 3D convolution.
  Pooling can be done in the separate "pool" layer.
  """

  layer_class = "conv"
  recurrent = True  # we must not allow any shuffling in the time-dim or so

  def __init__(self, n_out, filter_size, padding, strides=1, dilation_rate=1,
               input_expand_dims=0, input_add_feature_dim=False, input_split_feature_dim=None,
               with_bias=False,
               activation=None,
               **kwargs):
    """
    :param int n_out: number of outgoing features
    :param tuple[int] filter_size: (width,), (height,width) or (depth,height,width) for 1D/2D/3D conv.
      the input data ndim must match, or you can add dimensions via input_expand_dims or input_add_feature_dim.
      it will automatically swap the batch-dim to the first axis of the input data.
    :param str padding: "same" or "valid"
    :param int|tuple[int] strides: strides for the spatial dims,
      i.e. length of this tuple should be the same as filter_size, or a single int.
    :param int input_expand_dims: number of dynamic dims to add to the input
    :param bool input_add_feature_dim: will add a dim at the end and use input-feature-dim == 1,
      and use the original input feature-dim as a spatial dim.
    :param None|int input_split_feature_dim: if set, like input_add_feature_dim it will add a new feature dim
      which is of value input_split_feature_dim, and the original input feature dim
      will be divided by input_split_feature_dim, thus it must be a multiple of that value.
    :param bool with_bias: if True, will add a bias to the output features
    :param None|str activation: if set, will apply this function at the end
    """
    from TFUtil import check_input_dim, get_shape
    padding = padding.upper()
    assert padding in ["SAME", "VALID"], "no other padding supported at the moment"
    assert "out_type" not in kwargs, "don't set out_type explicitly for this layer"
    assert len(filter_size) in (1, 2, 3), "only 1D conv, 2D conv or 3D conv supported"
    super(ConvLayer, self).__init__(**kwargs)
    if isinstance(strides, int):
      strides = [strides] * len(filter_size)
    else:
      strides = list(strides)
    assert len(strides) == len(filter_size)
    if isinstance(dilation_rate, int):
      dilation_rate = [dilation_rate] * len(filter_size)
    else:
      dilation_rate = list(dilation_rate)
    assert len(dilation_rate) == len(filter_size)
    assert not self.input_data.sparse
    # We want to prepare the input data such that the batch-dim is the very first,
    # the feature-dim is the very last, and all in between are where we convolve over.
    # In the common terminology, this is the "NHWC" format, which is the default for TF convolution.
    x = self.input_data.get_placeholder_as_batch_major()
    x = check_input_dim(x, -1, self.input_data.dim)
    input_num_features = self.input_data.dim
    dyn_axes = self.input_data.get_dynamic_axes()  # conv-dims, or also called spatial dims
    static_axes = self.input_data.get_non_dynamic_axes()  # feature-dims
    assert dyn_axes + static_axes == list(range(self.input_data.ndim)), (
      "we expect the static dims at the end. input data is: %r" % self.input_data.get_description())
    if input_split_feature_dim:
      # Split the last two dimensions.
      assert self.input_data.dim % input_split_feature_dim == 0, "must be a multiple of the input feature dim"
      x = tf.reshape(
        x, get_shape(x)[:-1] + [self.input_data.dim // input_split_feature_dim, input_split_feature_dim])
      static_axes += [x.get_shape().ndims - 2]  # last without batch-dim
      input_num_features = input_split_feature_dim
    if input_add_feature_dim:
      # Add a dimension at the very end; any other static dims will be used as dynamic dims below.
      x = tf.expand_dims(x, axis=x.get_shape().ndims, name="input_use_feature_dim")
      static_axes += [x.get_shape().ndims - 2]  # last without batch-dim
      input_num_features = 1
    if len(static_axes) > 1:
      # Just treat them as dynamic axes, except the last.
      dyn_axes += static_axes[:-1]
      del static_axes[:-1]
    assert len(static_axes) == 1, "this should be our single input feature dim now. otherwise use input_add_feature_dim"
    while input_expand_dims:
      x = tf.expand_dims(x, axis=len(dyn_axes) + 1, name="input_expand_dims")  # axis including batch-dim
      dyn_axes += [len(dyn_axes)]
      static_axes = [axis + 1 for axis in static_axes]
      input_expand_dims -= 1
    assert dyn_axes == list(range(len(filter_size))), (
      "filter-size-dimension does not match the input data. " +
      "this is %i-D conv but number of spatial dims is %i in the input %s. " % (
        len(filter_size), len(dyn_axes), self.input_data.get_description()) +
      "consider using input_expand_dims or input_add_feature_dim.")
    filter_shape = list(filter_size) + [input_num_features, n_out]
    with var_creation_scope():
      filters = self.add_param(tf.Variable(
        name="W",
        initial_value=tf.contrib.layers.xavier_initializer(seed=self.network.random.randint(2**31))(
          shape=filter_shape)))
    y = tf.nn.convolution(x, filter=filters, padding=padding, strides=strides, dilation_rate=dilation_rate)
    # y shape is [batch] + dynamic_dims + [n_out].
    if with_bias:
      with var_creation_scope():
        b = self.add_param(tf.Variable(
          name="bias",
          initial_value=tf.constant_initializer(value=0, dtype=tf.float32)(
            shape=(n_out,))))
      y += b
    if activation:
      from TFUtil import get_activation_function
      act_func = get_activation_function(activation)
      self.output_before_activation = OutputWithActivation(y, act_func=act_func)
    else:
      self.output_before_activation = OutputWithActivation(y)
    y = self.output_before_activation.y
    self.output.placeholder = y
    self.output.size_placeholder = {
      i: (self.input_data.size_placeholder[i] if i in self.input_data.size_placeholder else tf.shape(y)[i + 1])
      for i in dyn_axes}
    if padding == "SAME":
      pass
    elif padding == "VALID":
      for i, s in list(self.output.size_placeholder.items()):
        self.output.size_placeholder[i] = s - filter_size[i] + 1
    else:
      assert False

  @classmethod
  def _get_out_type_from_opts(cls, n_out, filter_size, **kwargs):
    return {
      "dim": n_out,
      "shape": [None] * len(filter_size) + [n_out],
      "batch_dim_axis": 0,
      "sparse": False}

  @classmethod
  def get_out_data_from_opts(cls, **kwargs):
    out_type = cls._get_out_type_from_opts(**kwargs)
    return super(ConvLayer, cls).get_out_data_from_opts(out_type=out_type, **kwargs)


class PoolLayer(_ConcatInputLayer):
  """
  A generic N-D pooling layer.
  This would usually be done after a convolution for down-sampling.
  """

  layer_class = "pool"
  recurrent = True  # we should not shuffle in the time-dimension

  def __init__(self, mode, pool_size, padding="VALID", dilation_rate=1, strides=None, **kwargs):
    """
    :param str mode: "max" or "avg"
    :param tuple[int] pool_size: shape of the window of each reduce
    :param str padding: "valid" or "same"
    :param tuple[int]|int dilation_rate:
    :param tuple[int]|int|None strides: in contrast to tf.nn.pool, the default (if it is None) will be set to pool_size
    """
    assert "n_out" not in kwargs
    assert "out_type" not in kwargs
    from TFUtil import check_input_dim
    mode = mode.upper()
    assert mode in ["MAX", "AVG"]
    padding = padding.upper()
    assert padding in ["VALID", "SAME"]
    if isinstance(dilation_rate, int):
      dilation_rate = [dilation_rate] * len(pool_size)
    assert len(dilation_rate) == len(pool_size)
    if strides is None:
      strides = pool_size
    elif isinstance(strides, int):
      strides = [strides] * len(pool_size)
    assert len(strides) == len(pool_size)
    super(PoolLayer, self).__init__(**kwargs)
    # We want to prepare the input data such that the batch-dim is the very first,
    # the feature-dim is the very last, and all in between are where we convolve over.
    # In the common terminology, this is the "NHWC" format, which is the default for TF convolution/pooling.
    x = self.input_data.get_placeholder_as_batch_major()
    x = check_input_dim(x, -1, self.input_data.dim)
    y = tf.nn.pool(
      x, window_shape=pool_size, pooling_type=mode, padding=padding,
      dilation_rate=dilation_rate, strides=strides)
    # y shape is [batch] + spatial_dims + [n_out].
    self.output.placeholder = y
    self.output.size_placeholder = {
      i: (self.input_data.size_placeholder[i] if i in self.input_data.size_placeholder else tf.shape(y)[i + 1])
      for i in range(len(pool_size))}
    if padding == "SAME":
      pass
    elif padding == "VALID":
      for i, s in list(self.output.size_placeholder.items()):
        self.output.size_placeholder[i] = s - pool_size[i] + 1
    else:
      assert False

  @classmethod
  def get_out_data_from_opts(cls, name, pool_size, sources, **kwargs):
    # y shape is [batch] + spatial_dims + [n_out].
    input_data = get_concat_sources_data_template(sources)
    return Data(
      name="%s_output" % name,
      shape=(None,) * len(pool_size) + (input_data.dim,),
      dim=input_data.dim,
      dtype=input_data.dtype,
      sparse=False,
      batch_dim_axis=0)


class ReduceLayer(_ConcatInputLayer):
  layer_class = "reduce"

  def __init__(self, mode, axis, keep_dims=False, enforce_batch_dim_axis=0, **kwargs):
    """
    :param str mode: "sum" or "max"
    :param int|list[int]|str axis: one axis or multiple axis to reduce.
      this is counted with batch-dim, which by default is axis 0 (see enforce_batch_dim_axis).
      it also accepts the special tokens "B"|"batch", "spatial", "spatial_except_time", or "F"|"feature"
    :param bool keep_dims: if dimensions should be kept (will be 1)
    :param int enforce_batch_dim_axis: will swap the batch-dim-axis of the input with the given axis.
      e.g. 0: will convert the input into batch-major format if not already like that.
    """
    from TFUtil import swapaxes
    assert "n_out" not in kwargs
    assert "out_type" not in kwargs
    mode = mode.lower()
    assert mode in ["max", "sum", "avg", "mean"]
    super(ReduceLayer, self).__init__(**kwargs)
    assert not self.input_data.sparse
    x = self.input_data.placeholder
    if self.input_data.batch_dim_axis != enforce_batch_dim_axis:
      x = swapaxes(x, self.input_data.batch_dim_axis, enforce_batch_dim_axis)
    axis = self._get_axis(axis, input_data=self.input_data)
    if mode == "max":
      f = tf.reduce_max
    elif mode == "sum":
      f = tf.reduce_sum
    elif mode in ["avg", "mean"]:
      f = tf.reduce_mean
    else:
      assert False
    y = f(x, axis=axis, keep_dims=keep_dims)
    y_dyn_sizes = self.input_data.size_placeholder.copy()
    if keep_dims:
      for i in axis:
        if i in y_dyn_sizes:
          y_dyn_sizes[i] = 1
    else:
      for i in reversed(sorted(axis)):
        if i in y_dyn_sizes:
          del y_dyn_sizes[i]
        y_dyn_sizes = {(j if (j < i) else (j - 1)): s
                       for (j, s) in list(y_dyn_sizes.items())}
    self.output.placeholder = y
    self.output.size_placeholder = y_dyn_sizes

  @classmethod
  def _get_axis(cls, axis, input_data):
    """
    :param axis: see self.__init__()
    :param Data input_data:
    :return: list of axes
    :rtype: list[int]
    """
    if isinstance(axis, str):
      axis = axis.lower()
      if axis in ["b", "batch"]:
        axis = 0
      elif axis == "spatial":
        axis = input_data.get_dynamic_batch_axes()
        axis.remove(input_data.batch_dim_axis)
      elif axis == "spatial_except_time":
        axis = input_data.get_dynamic_batch_axes()
        axis.remove(input_data.batch_dim_axis)
        axis.remove(input_data.time_dim_axis)
      elif axis in ["f", "feature"]:
        axis = input_data.get_non_dynamic_axes()
      else:
        raise Exception("invalid axis mode %r" % axis)
    if isinstance(axis, int):
      axis = [axis]
    assert isinstance(axis, (tuple, list)), "invalid axis %r" % axis
    assert len(axis) > 0, "no axis to reduce. input_data: %s" % (input_data,)
    axis = [i % input_data.batch_ndim for i in axis]
    return axis

  @classmethod
  def get_out_data_from_opts(cls, name, sources, axis, keep_dims=False, **kwargs):
    input_data = get_concat_sources_data_template(sources)
    axis = cls._get_axis(axis=axis, input_data=input_data)
    y_shape = list(input_data.batch_shape)
    if keep_dims:
      for i in axis:
        y_shape[i] = 1
    else:
      for i in reversed(sorted(axis)):
        del y_shape[i]
    return Data(
      name="%s_output" % name,
      shape=tuple(y_shape[1:]),
      dtype=input_data.dtype,
      sparse=False)


class GetLastHiddenStateLayer(LayerBase):
  """
  Will combine (concat or add or so) all the last hidden states from all sources.
  """

  layer_class = "get_last_hidden_state"

  def __init__(self, n_out, combine="concat", **kwargs):
    """
    :param int n_out: dimension. output will be of shape (batch, n_out)
    :param str combine: "concat" or "add"
    """
    super(GetLastHiddenStateLayer, self).__init__(**kwargs)
    assert len(self.sources) > 0
    sources = [s.get_last_hidden_state() for s in self.sources]
    assert all([s is not None for s in sources])
    if len(sources) == 1:
      h = sources[0]
    else:
      if combine == "concat":
        h = tf.concat(sources, axis=1, name="concat_hidden_states")
      elif combine == "add":
        h = tf.add_n(sources, name="add_hidden_states")
      else:
        raise Exception("invalid hidden states combine mode %r" % combine)
    from TFUtil import check_input_ndim, check_input_dim
    h = check_input_ndim(h, 2)
    h = check_input_dim(h, 1, n_out)
    self.output.placeholder = h

  def get_last_hidden_state(self):
    return self.output.placeholder

  @classmethod
  def get_out_data_from_opts(cls, n_out, **kwargs):
    return super(GetLastHiddenStateLayer, cls).get_out_data_from_opts(
      out_type={"shape": (n_out,), "dim": n_out, "batch_dim_axis": 0, "time_dim_axis": None}, **kwargs)


class RnnCellLayer(_ConcatInputLayer):
  """
  Wrapper around tf.contrib.rnn.RNNCell.
  This will operate a single step, i.e. there is no time dimension,
  i.e. we expect a (batch,n_in) input, and our output is (batch,n_out).
  """

  layer_class = "rnn_cell"

  def __init__(self, n_out, unit, prev_state, unit_opts=None, **kwargs):
    """
    :param int n_out: so far, only output shape (batch,n_out) supported
    :param str|tf.contrib.rnn.RNNCell unit: e.g. "BasicLSTM" or "LSTMBlock"
    :param tf.Tensor prev_state:
    :param dict[str]|None unit_opts:
    """
    super(RnnCellLayer, self).__init__(**kwargs)
    with tf.variable_scope(
          "rec",
          initializer=tf.contrib.layers.xavier_initializer(
            seed=self.network.random.randint(2**31))) as scope:
      assert isinstance(scope, tf.VariableScope)
      scope_name_prefix = scope.name + "/"  # e.g. "layer1/rec/"
      self.cell = self._get_cell(n_out=n_out, unit=unit, unit_opts=unit_opts)
      self.output.time_dim_axis = None
      self.output.batch_dim_axis = 0
      self.output.placeholder, state = self.cell(self.input_data.placeholder, prev_state)
      self._hidden_state = state
      params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope_name_prefix)
      assert params
      self.params.update({p.name[len(scope_name_prefix):-2]: p for p in params})

  @classmethod
  def _get_cell(cls, n_out, unit, unit_opts=None):
    """
    :param int n_out:
    :param str|tf.contrib.rnn.RNNCell unit:
    :param dict[str]|None unit_opts:
    :rtype: tf.contrib.rnn.RNNCell
    """
    import tensorflow.contrib.rnn as rnn_contrib
    if isinstance(unit, rnn_contrib.RNNCell):
      return unit
    rnn_cell_class = RecLayer.get_rnn_cell_class(unit)
    assert issubclass(rnn_cell_class, rnn_contrib.RNNCell)
    if unit_opts is None:
      unit_opts = {}
    assert isinstance(unit_opts, dict)
    # This should not have any side-effects, i.e. it should not add to the current computation graph,
    # it should also not create any vars yet, etc.
    cell = rnn_cell_class(n_out, **unit_opts)
    assert isinstance(cell, rnn_contrib.RNNCell)
    return cell

  @classmethod
  def get_out_data_from_opts(cls, n_out, name, **kwargs):
    return Data(
      name="%s_output" % name,
      shape=(n_out,), dim=n_out,
      batch_dim_axis=0,
      time_dim_axis=None,
      size_placeholder={})

  @classmethod
  def get_hidden_state_size(cls, n_out, unit, unit_opts=None, **kwargs):
    cell = cls._get_cell(unit=unit, unit_opts=unit_opts, n_out=n_out)
    import tensorflow.contrib.rnn as rnn_contrib
    assert isinstance(cell, rnn_contrib.RNNCell)
    return cell.state_size

  def get_hidden_state(self):
    return self._hidden_state

  def get_last_hidden_state(self):
    from tensorflow.python.util import nest
    if nest.is_sequence(self._hidden_state):
      return tf.concat(self._hidden_state, axis=1)
    return self._hidden_state


class AttentionBaseLayer(_ConcatInputLayer):
  """
  This is the base class for attention.
  This layer would get constructed in the context of one single decoder step.
  We get the whole encoder output over all encoder frames (the base), e.g. (batch,enc_time,enc_dim),
  and some current decoder context, e.g. (batch,dec_att_dim),
  and we are supposed to return the attention output, e.g. (batch,att_dim).
  """

  def __init__(self, base, base_ctx, **kwargs):
    """
    :param LayerBase base: encoder output to attend on
    :param LayerBase base_ctx: encoder output used to calculate the attention weights
    """
    super(AttentionBaseLayer, self).__init__(**kwargs)
    self.base = base
    self.base_ctx = base_ctx

  @classmethod
  def transform_config_dict(cls, d, network, get_layer):
    super(AttentionBaseLayer, cls).transform_config_dict(d, network=network, get_layer=get_layer)
    d["base"] = get_layer(d["base"])
    d["base_ctx"] = get_layer(d["base_ctx"])

  @classmethod
  def get_out_data_from_opts(cls, base, **kwargs):
    """
    :param LayerBase base:
    """
    return base.output.copy_template_excluding_time_dim()


class GlobalAttentionContextLayer(AttentionBaseLayer):
  layer_class = "global_attention_context"

  def __init__(self, **kwargs):
    super(GlobalAttentionContextLayer, self).__init__(**kwargs)
    # We expect input_data of shape (batch, inner),
    # base_ctx of shape (batch, base_time, inner) and base of shape (batch, base_time, n_out).
    assert self.input_data.batch_ndim == 2
    assert self.input_data.time_dim_axis is None
    assert self.base.output.batch_ndim == 3
    assert self.base_ctx.output.batch_ndim == 3
    assert self.input_data.dim == self.base_ctx.output.dim
    # And we want to do a dot product so that we get (batch, base_time).
    with tf.name_scope("att_energy"):
      # Get base of shape (batch, base_time, inner).
      base = self.base.output.get_placeholder_as_batch_major()  # (batch, base_time, n_out)
      base_ctx = self.base_ctx.output.get_placeholder_as_batch_major()  # (batch, base_time, inner)
      # Get source of shape (batch, inner, 1).
      source = tf.expand_dims(self.input_data.placeholder, axis=2)
      energy = tf.matmul(base_ctx, source)
      energy.set_shape(tf.TensorShape([None, None, 1]))  # (batch, base_time, 1)
      energy = tf.squeeze(energy, axis=2)  # (batch, base_time)
      base_weights = tf.nn.softmax(energy)  # (batch, base_time)
      base_weights_bc = tf.expand_dims(base_weights, axis=1)  # (batch, 1, base_time)
      out = tf.matmul(base_weights_bc, base)  # (batch, 1, n_out)
      out.set_shape(tf.TensorShape([None, 1, self.output.dim]))
      out = tf.squeeze(out, axis=1)  # (batch, n_out)
      self.output.placeholder = out
      self.output.size_placeholder = {}


class RecLayer(_ConcatInputLayer):
  layer_class = "rec"
  recurrent = True
  _rnn_cells_dict = {}

  @classmethod
  def _create_rnn_cells_dict(cls):
    import tensorflow.contrib.rnn as rnn_contrib
    import TFNativeOp
    def maybe_add(key, v):
      if isinstance(v, type) and issubclass(v, (rnn_contrib.RNNCell, rnn_contrib.FusedRNNCell, TFNativeOp.RecSeqCellOp)):
        name = key
        if name.endswith("Cell"):
          name = name[:-len("Cell")]
        name = name.lower()
        assert cls._rnn_cells_dict.get(name) in [v, None]
        cls._rnn_cells_dict[name] = v
    for key, v in vars(rnn_contrib).items():
      maybe_add(key, v)
    for key, v in vars(TFNativeOp).items():
      maybe_add(key, v)

  @classmethod
  def get_rnn_cell_class(cls, name):
    """
    :param str name: cell name, minus the "Cell" at the end
    :rtype: () -> tensorflow.contrib.rnn.RNNCell
    """
    if not cls._rnn_cells_dict:
      cls._create_rnn_cells_dict()
    return cls._rnn_cells_dict[name.lower()]

  @classmethod
  def transform_config_dict(cls, d, network, get_layer):
    """
    :param dict[str] d: will modify inplace
    :param TFNetwork.TFNetwork network:
    :param ((str) -> LayerBase) get_layer: function to get or construct another layer
    """
    super(RecLayer, cls).transform_config_dict(d, network=network, get_layer=get_layer)
    initial_state = d.pop("initial_state", None)
    if initial_state:
      if not isinstance(initial_state, (list, tuple)):
        initial_state = [initial_state]
      assert len(initial_state) > 0
      sources = [get_layer(s).get_last_hidden_state() for s in initial_state]
      if len(sources) == 1:
        d["initial_state"] = sources[0]
      else:
        d["initial_state"] = tf.concat(sources, axis=1, name="concat_hidden_state")
    if isinstance(d.get("unit"), dict):
      def sub_get_layer(name):
        if name.startswith("base:"):
          return get_layer(name[len("base:"):])
      for sub in d["unit"].values():
        assert isinstance(sub, dict)
        if "initial_state" in sub:
          assert(sub["initial_state"], str)
          if sub["initial_state"].startswith("base:"):
            sub["initial_state"] = get_layer(sub["initial_state"][len("base:"):]).get_last_hidden_state()
        if "class" in sub:
          class_name = sub["class"]
          cl = get_layer_class(class_name)
          # Operate on a copy because we will transform the dict later.
          # We only need this to resolve any other layer dependencies in the main network.
          cl.transform_config_dict(sub.copy(), network=network, get_layer=sub_get_layer)

  def __init__(self,
               unit="lstm",
               direction=None, input_projection=True,
               initial_state=None,
               **kwargs):
    """
    :param str|dict[str,dict[str]] unit: the RNNCell/etc name, e.g. "nativelstm". see comment below.
      alternatively a whole subnetwork, which will be executed step by step,
      and which can include "prev" in addition to "from" to refer to previous steps.
    :param int|None direction: None|1 -> forward, -1 -> backward
    :param bool input_projection: True -> input is multiplied with matrix. False only works if same input dim
    :param tf.Tensor|None initial_state:
    :param dict[str]|bool output_feed_back: feedback mode opts, see OutputFeedback
    :param dict[str]|bool attention: attention opts, see Attention
    :param dict[str] kwargs: passed on to base class
    """
    super(RecLayer, self).__init__(**kwargs)
    import tensorflow.contrib.rnn as rnn_contrib
    import TFNativeOp
    if direction is not None:
      assert direction in [-1, 1]
    self._last_hidden_state = None
    self._direction = direction
    self._initial_state = initial_state
    self._input_projection = input_projection
    with tf.variable_scope(
          "rec",
          initializer=tf.contrib.layers.xavier_initializer(
            seed=self.network.random.randint(2**31))) as scope:
      assert isinstance(scope, tf.VariableScope)
      self._rec_scope = scope
      scope_name_prefix = scope.name + "/"  # e.g. "layer1/rec/"
      self.cell = self._get_cell(unit)
      if isinstance(self.cell, (rnn_contrib.RNNCell, rnn_contrib.FusedRNNCell)):
        y = self._get_output_cell(self.cell)
      elif isinstance(self.cell, TFNativeOp.RecSeqCellOp):
        y = self._get_output_native_rec_op(self.cell)
      elif isinstance(self.cell, self.SubnetworkCell):
        y = self._get_output_subnet_unit(self.cell)
      else:
        raise Exception("invalid type: %s" % type(self.cell))
      self.output.time_dim_axis = 0
      self.output.batch_dim_axis = 1
      self.output.placeholder = y
      params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope_name_prefix)
      assert params
      self.params.update({p.name[len(scope_name_prefix):-2]: p for p in params})

  def _get_input(self):
    """
    :return: (x, seq_len), where x is (time,batch,...,dim) and seq_len is (batch,)
    :rtype: (tf.Tensor, tf.Tensor)
    """
    assert self.input_data
    x = self.input_data.placeholder  # (batch,time,dim) or (time,batch,dim)
    if not self.input_data.is_time_major:
      assert self.input_data.batch_dim_axis == 0
      assert self.input_data.time_dim_axis == 1
      from TFUtil import swapaxes
      x = swapaxes(x, 0, 1)  # (time,batch,[dim])
    seq_len = self.input_data.size_placeholder[0]
    return x, seq_len

  def _get_cell(self, unit):
    import tensorflow.contrib.rnn as rnn_contrib
    import TFNativeOp
    if isinstance(unit, dict):
      return self.SubnetworkCell(parent=self, net_dict=unit)
    else:
      assert isinstance(unit, str)
      if unit.lower() in ["lstmp", "lstm"]:
        # Some possible LSTM implementations are (in all cases for both CPU and GPU):
        # * BasicLSTM (the cell), via official TF, pure TF implementation
        # * LSTMBlock (the cell), via tf.contrib.rnn.
        # * LSTMBlockFused, via tf.contrib.rnn. should be much faster than BasicLSTM
        # * NativeLSTM, our own native LSTM. should be faster than LSTMBlockFused
        # We default to the fastest one, i.e. NativeLSTM.
        # Note that they are currently not compatible to each other, i.e. the way the parameters are represented.
        unit = "nativelstm"  # TFNativeOp.NativeLstmCell
    rnn_cell_class = self.get_rnn_cell_class(unit)
    n_hidden = self.output.dim
    cell = rnn_cell_class(n_hidden)
    assert isinstance(
      cell, (rnn_contrib.RNNCell, rnn_contrib.FusedRNNCell, TFNativeOp.RecSeqCellOp))  # e.g. BasicLSTMCell
    return cell

  def _get_output_cell(self, cell):
    from tensorflow.python.ops import rnn
    import tensorflow.contrib.rnn as rnn_contrib
    assert self.input_data
    assert not self.input_data.sparse
    x, seq_len = self._get_input()
    if self._direction == -1:
      x = tf.reverse_sequence(x, seq_lengths=seq_len, batch_dim=1, seq_dim=0)
    if isinstance(cell, rnn_contrib.RNNCell):  # e.g. BasicLSTMCell
      # Will get (time,batch,ydim).
      y, final_state = rnn.dynamic_rnn(
        cell=cell, inputs=x, time_major=True, sequence_length=seq_len, dtype=tf.float32,
        initial_state=self._initial_state)
      self._last_hidden_state = final_state
    elif isinstance(cell, rnn_contrib.FusedRNNCell):  # e.g. LSTMBlockFusedCell
      # Will get (time,batch,ydim).
      y, final_state = cell(
        inputs=x, sequence_length=seq_len, dtype=tf.float32,
        initial_state=self._initial_state)
      self._last_hidden_state = final_state
    else:
      raise Exception("invalid type: %s" % type(cell))
    if self._direction == -1:
      y = tf.reverse_sequence(y, seq_lengths=seq_len, batch_dim=1, seq_dim=0)
    return y

  def _get_output_native_rec_op(self, cell):
    from TFUtil import dot, sequence_mask_time_major, directed
    assert self.input_data
    x, seq_len = self._get_input()
    if self._input_projection:
      W = tf.get_variable(name="W", shape=(self.input_data.dim, cell.n_input_dim), dtype=tf.float32)
      if self.input_data.sparse:
        x = tf.nn.embedding_lookup(W, x)
      else:
        x = dot(x, W)
    else:
      assert not self.input_data.sparse
      assert self.input_data.dim == cell.n_input_dim
    b = tf.get_variable(name="b", shape=(cell.n_input_dim,), dtype=tf.float32, initializer=tf.constant_initializer(0.0))
    x += b
    index = sequence_mask_time_major(seq_len, maxlen=tf.shape(x)[0])
    y, final_state = cell(
      inputs=directed(x, self._direction), index=directed(index, self._direction),
      initial_state=self._initial_state)
    self._last_hidden_state = final_state
    y = directed(y, self._direction)
    return y

  class SubnetworkCell(object):
    def __init__(self, parent, net_dict):
      """
      :param RecLayer parent:
      :param dict[str] net_dict:
      """
      from copy import deepcopy
      self.parent = parent
      self.net_dict = deepcopy(net_dict)
      self._net_dict_extra = {}  # type: dict[str,dict[str]]
      self._extract_extra()
      from TFNetwork import TFNetwork, ExternData
      self.net = TFNetwork(extern_data=ExternData(), train_flag=parent.network.train_flag, parent=parent)
      if parent.input_data:
        self.net.extern_data.data["source"] = \
          parent.input_data.copy_template_excluding_time_dim()
      if parent.target:
        self.net.extern_data.data[parent.target] = \
          parent.network.extern_data.data[parent.target].copy_template_excluding_time_dim()
      self.layer_data_templates = {}  # type: dict[str,RecLayer.SubnetworkCell.TemplateLayer]
      self.prev_layers_needed = set()  # type: set[str]
      self.layers_with_hidden_state = set()  # type: set[str]
      self._construct_template()

    def _extract_extra(self):
      keys = ["initial_state", "initial_output"]
      for name, d in list(self.net_dict.items()):
        assert isinstance(d, dict)
        self._net_dict_extra[name] = {}
        for k in keys:
          if k in d:
            self._net_dict_extra[name][k] = d.pop(k)
        if not d:
          del self.net_dict[name]

    class TemplateLayer(LayerBase):
      def __init__(self, network, name):
        """
        :param TFNetwork.TFNetwork network:
        :param str name:
        """
        # Init with some dummy.
        super(RecLayer.SubnetworkCell.TemplateLayer, self).__init__(
          out_type={"shape": ()}, name=name, network=network)
        self.output.size_placeholder = {}  # must be initialized
        self.layer_class = ":uninitialized-template"
        self._layer_class = None
        self._kwargs = None

      def init(self, output, layer_class, template_type="template", **kwargs):
        """
        :param Data output: 
        :param type[LayerBase]|LayerBase layer_class: 
        :param str template_type: 
        """
        self.layer_class = ":%s:%s" % (template_type, layer_class.layer_class)
        self.output = output
        if not self.output.size_placeholder:
          self.output.size_placeholder = {}
        self._layer_class = layer_class
        self._kwargs = kwargs
        self._kwargs["output"] = output

      def copy(self, template_type):
        """
        :param str template_type: e.g. "prev"
        :return: new _TemplateLayer
        :rtype: RecLayer.SubnetworkCell.TemplateLayer
        """
        l = self.__class__(network=self.network, name=self.name)
        l.init(layer_class=self._layer_class, template_type=template_type, **self._kwargs)
        return l

    def _construct_template(self):
      """
      Without creating any computation graph, create TemplateLayer instances.
      """
      def add_templated_layer(name, layer_class, **layer_desc):
        """
        :param str name:
        :param type[LayerBase]|LayerBase layer_class:
        :param dict[str] layer_desc:
        :rtype: LayerBase
        """
        layer = self.layer_data_templates[name]
        layer_desc = layer_desc.copy()
        layer_desc["name"] = name
        layer_desc["network"] = self.net
        output = layer_class.get_out_data_from_opts(**layer_desc)
        if issubclass(layer_class, RnnCellLayer):
          self.layers_with_hidden_state.add(name)
        layer.init(layer_class=layer_class, output=output, **layer_desc)
        return self.layer_data_templates[name]

      def get_templated_layer(name):
        """
        :param str name:
        :rtype: RecLayer.SubnetworkCell.TemplateLayer|LayerBase
        """
        if name in self.layer_data_templates:
          return self.layer_data_templates[name]
        if name.startswith("prev:"):
          name = name[len("prev:"):]
          self.prev_layers_needed.add(name)
        if name.startswith("base:"):
          return self.parent.network.layers[name[len("base:"):]]
        # Need to create layer instance here now to not run into recursive loops.
        # We will extend it later in add_layer().
        self.layer_data_templates[name] = self.TemplateLayer(name=name, network=self.net)
        return self.net._construct_layer(
          self.net_dict, name, get_layer=get_templated_layer, add_layer=add_templated_layer)

      assert not self.layer_data_templates, "do not call this multiple times"
      get_templated_layer("output")
      assert "output" in self.layer_data_templates

    def _construct(self, prev_outputs, prev_states, data=None, classes=None):
      """
      :param dict[str,tf.Tensor] prev_outputs: outputs of the layers from the previous step
      :param dict[str,tf.Tensor] prev_states: hidden states of the previous step for layers who need it
      :param tf.Tensor|None data: optional source data, shape e.g. (batch,dim)
      :param tf.Tensor|None classes: optional target classes, shape e.g. (batch,) if it is sparse
      """
      if data is not None:
        self.net.extern_data.data["source"].placeholder = data
      if classes is not None:
        self.net.extern_data.data[self.parent.target].placeholder = classes

      prev_layers = {}  # type: dict[str,RecLayer.SubnetworkCell.TemplateLayer]
      for name in prev_outputs.keys():
        layer = prev_layers[name] = self.layer_data_templates[name].copy(template_type="prev")
        layer.output.placeholder = prev_outputs[name]
        layer.output.placeholder.set_shape(tf.TensorShape(layer.output.batch_shape))
        layer.output.size_placeholder = {}  # must be set

      from copy import deepcopy
      net_dict = deepcopy(self.net_dict)
      for name in prev_states.keys():
        net_dict[name]["prev_state"] = prev_states[name]

      def get_layer(name):
        if name.startswith("prev:"):
          return prev_layers[name[len("prev:"):]]
        if name.startswith("base:"):
          return self.parent.network.layers[name[len("base:"):]]
        return self.net._construct_layer(net_dict, name=name, get_layer=get_layer)

      assert not self.net.layers, "do not call this multiple times"
      get_layer("output")
      assert "output" in self.net.layers
      # Might not be resolved otherwise:
      for name in self.prev_layers_needed:
        get_layer(name)

    def _get_init_output(self, name, batch_dim):
      """
      :param str name: layer name
      :param tf.Tensor batch_dim:
      :rtype: tf.Tensor
      """
      data = self.layer_data_templates[name].output
      # Warning: Should we silently allow this and use the default (0) for sparse data?
      v = self._net_dict_extra.get(name, {}).get("initial_output", None)
      if isinstance(v, tf.Tensor):
        return v
      if v is None and data.sparse:
        raise Exception(
          ("You must explicitly provide an initial output value for sparse data %r." % data) +
          (" E.g. '%s': {'initial_output': 'zeros'}." % name))
      if v is None:
        v = "zeros"
      assert isinstance(v, str)
      shape = [batch_dim] + list(data.shape)
      if v == "zeros":
        return tf.zeros(shape, dtype=data.dtype)
      elif v == "ones":
        return tf.ones(shape, dtype=data.dtype)
      else:
        raise Exception("invalid initial output type %r for sub-layer %r" % (v, name))

    def _get_init_state(self, name, batch_dim):
      """
      :param str name: layer name
      :param tf.Tensor batch_dim:
      :rtype: tf.Tensor|tuple[tf.Tensor]
      """
      v = self._net_dict_extra[name].get("initial_state", "zeros")
      if isinstance(v, tf.Tensor):
        return v
      assert isinstance(v, str)
      cl = self.layer_data_templates[name]._layer_class
      assert issubclass(cl, RnnCellLayer)
      dim = cl.get_hidden_state_size(**self.layer_data_templates[name]._kwargs)
      def make(d):
        assert isinstance(d, int)
        shape = [batch_dim, d]
        if v == "zeros":
          return tf.zeros(shape)
        elif v == "ones":
          return tf.ones(shape)
        else:
          raise Exception("invalid initial state type %r for sub-layer %r" % (v, name))
      if isinstance(dim, (tuple, list)):
        return tuple([make(d) for d in dim])
      return make(dim)

    def get_next_loop_vars(self, loop_vars, data=None, classes=None):
      """
      :param (list[tf.Tensor],list[tf.Tensor]) loop_vars: loop_vars from the previous step
      :param tf.Tensor|None data: optional source data, shape e.g. (batch,dim)
      :param tf.Tensor|None classes: optional target classes, shape e.g. (batch,) if it is sparse
      :return: next loop_vars
      :rtype: (list[tf.Tensor],list[tf.Tensor|tuple[tf.Tensor]])
      """
      prev_outputs_flat, prev_hidden_states_flat = loop_vars
      assert len(prev_outputs_flat) == len(self.prev_layers_needed)
      prev_outputs = {k: v for (k, v) in zip(sorted(self.prev_layers_needed), prev_outputs_flat)}
      assert len(prev_hidden_states_flat) == len(self.layers_with_hidden_state)
      prev_states = {k: v for (k, v) in zip(sorted(self.layers_with_hidden_state), prev_hidden_states_flat)}
      with reuse_name_scope(self.parent._rec_scope):
        self._construct(prev_outputs=prev_outputs, prev_states=prev_states, data=data, classes=classes)
      outputs_flat = [self.net.layers[k].output.placeholder for k in sorted(self.prev_layers_needed)]
      states_flat = [self.net.layers[k].get_hidden_state() for k in sorted(self.layers_with_hidden_state)]
      return outputs_flat, states_flat

    def get_init_loop_vars(self, batch_dim):
      """
      :param tf.Tensor batch_dim: 
      :return: initial loop_vars. see self.get_next_loop_vars()
      :rtype: (list[tf.Tensor],list[tf.Tensor|tuple[tf.Tensor]])
      """
      init_outputs_flat = [self._get_init_output(k, batch_dim=batch_dim) for k in sorted(self.prev_layers_needed)]
      init_states_flat = [self._get_init_state(k, batch_dim=batch_dim) for k in sorted(self.layers_with_hidden_state)]
      return init_outputs_flat, init_states_flat

  def _get_output_subnet_unit(self, cell):
    """
    :param RecLayer.SubnetworkCell cell:
    """
    if self.input_data:
      with tf.name_scope("x_tensor_array"):
        x, seq_len = self._get_input()  # x will be (time,batch,..,dim)
        x_shape = tf.shape(x)
        x_ta = tf.TensorArray(
          dtype=self.input_data.dtype,
          element_shape=tf.TensorShape(self.input_data.copy_template_excluding_time_dim().batch_shape),
          size=x_shape[0],
          infer_shape=True)
        x_ta = x_ta.unstack(x)
      with tf.name_scope("batch_dim"):
        batch_dim = x_shape[1]
    else:
      x_ta = None
      seq_len = self.output.size_placeholder[0]  # see BaseLayer.__init__()
      data = self.network.extern_data.get_default_input_data()
      with tf.name_scope("batch_dim"):
        batch_dim = tf.shape(data.placeholder)[data.batch_dim_axis]

    # TODO: Better check for train_flag.
    # Maybe more generic via sampling options later.
    y_ta = None
    if self.target and self.network.train_flag is not False:
      y_data = self.network.get_extern_data(self.target, mark_data_key_as_used=True)
      y = y_data.get_placeholder_as_time_major()
      y_ta = tf.TensorArray(
        dtype=y_data.dtype,
        element_shape=tf.TensorShape(y_data.copy_template_excluding_time_dim().batch_shape),
        size=tf.shape(y)[0],
        infer_shape=True)
      y_ta = y_ta.unstack(y)

    # Note: tf.while_loop() will not give us all intermediate outputs, but we want them.
    # tf.scan() would do that but tf.scan() will loop over some input sequence -
    # however, that would not work because the input sequence is not fixed initially.
    # So, similar as tf.scan() does it, we collect all intermediate values.

    # In the while-loop, what we need to output is:
    # * next step counter (i)
    # * all outputs from layers which are in self.prev_layers_needed
    # * all hidden states from RnnCellLayer
    # * accumulated TensorArray of outputs from the output-layer for each step
    # For each of this, we need a sensible init, which we are supposed to return here.

    init_net_vars = cell.get_init_loop_vars(batch_dim=batch_dim)
    init_i = tf.constant(0)
    max_out_len = tf.reduce_max(seq_len)
    min_out_len = max_out_len
    # Create a tensor array to store the intermediate values for each step i, e.g. of shape (batch, dim).
    init_acc_ta = tf.TensorArray(
      dtype=cell.layer_data_templates["output"].output.dtype,
      element_shape=tf.TensorShape(cell.layer_data_templates["output"].output.batch_shape),
      size=min_out_len,
      dynamic_size=True,  # we will automatically grow it when needed
      infer_shape=True)

    def body(i, net_vars, acc_ta):
      """
      The loop body of scan.

      :param tf.Tensor i: loop counter, scalar
      :param net_vars: the accumulator values
      :param tf.TensorArray acc_ta: the output accumulator TensorArray
      :return: [i + 1, a_flat, tas]: the updated counter + new accumulator values + updated TensorArrays
      :rtype: (tf.Tensor, object, tf.TensorArray)

      Raises:
        TypeError: if initializer and fn() output structure do not match
        ValueType: if initializer and fn() output lengths do not match
      """
      with reuse_name_scope(self._rec_scope.name + "/while_loop_body"):
        net_vars = cell.get_next_loop_vars(
          net_vars,
          data=x_ta.read(i) if x_ta else None,
          classes=y_ta.read(i) if y_ta else None)
        acc_ta = acc_ta.write(i, cell.net.layers["output"].output.placeholder)
        return i + 1, net_vars, acc_ta

    def cond(i, net_vars, acc_ta):
      return tf.less(i, max_out_len)

    _, _, final_acc_ta = tf.while_loop(cond=cond, body=body, loop_vars=(init_i, init_net_vars, init_acc_ta))
    result = final_acc_ta.stack()  # e.g. (time, batch, dim)
    return result

  def get_last_hidden_state(self):
    assert self._last_hidden_state is not None, (
      "last-hidden-state not implemented/supported for this layer-type. try another unit. see the code.")
    return self._last_hidden_state


class FsaLayer(LayerBase):
  layer_class = "fsa"

  def __init__(self, **kwargs):
    """
    """
    super(FsaLayer, self).__init__(**kwargs)
    # TODO...


class CombineLayer(LayerBase):
  layer_class = "combine"

  def _check_same_dense_dim(self, sources):
    """
    :param list[LayerBase] sources:
    """
    assert not self.output.sparse
    for source in sources:
      assert not source.output.sparse
      assert source.output.dim == self.output.dim

  # Requires the same input shape and yield the same output shape.
  def _op_kind_add(self, sources):
    """
    :param list[LayerBase] sources:
    :rtype: tf.Tensor
    """
    self._check_same_dense_dim(sources)
    from TFUtil import swapaxes
    x = sources[0].output.placeholder
    batch_axis = sources[0].output.batch_dim_axis
    for source in sources[1:]:
      x2 = source.output.placeholder
      if source.output.batch_dim_axis != batch_axis:
        x2 = swapaxes(x2, batch_axis, source.output.batch_dim_axis)
      x += x2
    return x

  # Requires the same input shape and yield the same output shape.
  def _op_kind_average(self, sources):
    """
    :param list[LayerBase] sources:
    :rtype: tf.Tensor
    """
    x = self._op_kind_add(sources)
    x /= len(sources)
    return x

  def __init__(self, kind, sources, activation=None, with_bias=False, **kwargs):
    """
    :param str kind: e.g. "average"
    :param list[LayerBase] sources:
    :param str|None activation: if provided, activation function to apply, e.g. "tanh" or "relu"
    :param bool with_bias: if given , will add a bias
    """
    assert sources
    super(CombineLayer, self).__init__(sources=sources, **kwargs)
    op = getattr(self, "_op_kind_%s" % kind)
    x = op(sources)
    if with_bias:
      with var_creation_scope():
        b = self.add_param(tf.Variable(
          name="b",
          initial_value=tf.constant_initializer(value=0, dtype=tf.float32)(
            shape=(self.output.dim,))))
      x += b
    if activation:
      from TFUtil import get_activation_function
      act_func = get_activation_function(activation)
      self.output_before_activation = OutputWithActivation(x, act_func=act_func)
    else:
      self.output_before_activation = OutputWithActivation(x)
    x = self.output_before_activation.y
    self.output.placeholder = x

  @classmethod
  def get_out_data_from_opts(cls, n_out=None, out_type=None, sources=(), **kwargs):
    if not n_out and not out_type:
      out_type = sources[0].output.get_kwargs()
      out_type["name"] = "%s_output" % kwargs["name"]
    return super(CombineLayer, cls).get_out_data_from_opts(n_out=n_out, out_type=out_type, sources=sources, **kwargs)


class SubnetworkLayer(LayerBase):
  """
  You can define a whole subnetwork as a single layer by this class.
  """

  layer_class = "subnetwork"
  recurrent = True  # we don't know. depends on the subnetwork.

  def __init__(self, subnetwork, concat_sources=True, **kwargs):
    """
    :param dict[str,dict] network: subnetwork as dict (JSON content). must have an "output" layer
    :param bool concat_sources: if we concatenate all sources into one, like it is standard for most other layers
    """
    super(SubnetworkLayer, self).__init__(**kwargs)
    from TFNetwork import TFNetwork, ExternData
    sub_extern_data = ExternData()
    if concat_sources:
      sub_extern_data.data[sub_extern_data.default_input] = \
        concat_sources_with_opt_dropout(self.sources, dropout=kwargs.get("dropout", 0))
    else:
      assert not kwargs.get("dropout", 0), "not supported without concat_sources"
      for source in self.sources:
        assert isinstance(source, LayerBase)
        sub_extern_data.data[source.name] = source.output
    net = TFNetwork(
      rnd_seed=self.network.random.randint(2**31),
      train_flag=self.network.train_flag,
      extern_data=sub_extern_data,
      parent=self)
    net.construct_from_dict(subnetwork)
    self.subnetwork = net
    self.output = net.get_default_output_layer().output
    for layer in net.layers.values():
      assert layer.trainable == self.trainable, "partly trainable subnetworks not yet supported"
      self.params.update({"%s/%s" % (layer.name, k): v for (k, v) in layer.params.items()})

  @classmethod
  def get_out_data_from_opts(cls, subnetwork, n_out=None, out_type=None, **kwargs):
    """
    :param dict[str,dict[str]] subnetwork:
    :param int|None n_out:
    :param dict[str]|None out_type:
    :rtype: Data
    """
    if n_out or out_type:
      return super(SubnetworkLayer, cls).get_out_data_from_opts(n_out=n_out, out_type=out_type, **kwargs)
    layer_desc = subnetwork["output"].copy()
    class_name = layer_desc.pop("class")
    layer_class = get_layer_class(class_name)
    def _get_layer(name):
      raise Exception("not available at this point; provide n_out or out_type explicitly.")
    layer_desc["from"] = []  # that wont work here
    layer_class.transform_config_dict(layer_desc, get_layer=_get_layer, network=kwargs["network"])
    layer_desc["name"] = "output"
    layer_desc["network"] = None
    # Note: This can likely fail because we don't provide all the right args.
    # In that case, you must provide n_out or out_type explicitly.
    return layer_class.get_out_data_from_opts(**layer_desc)

  def get_constraints_value(self):
    self.subnetwork.maybe_construct_objective()
    v = self.subnetwork.total_constraints
    if v is 0:
      return None
    return v

  def get_loss_value(self):
    self.subnetwork.maybe_construct_objective()
    v = self.subnetwork.total_loss
    if v is 0:
      return None
    return v

  def get_error_value(self):
    self.subnetwork.maybe_construct_objective()
    errors = self.subnetwork.get_all_errors()
    if not errors:
      return None
    if len(errors) == 1:
      return list(errors.values())[0]
    name = self.subnetwork.get_default_output_layer_name()
    if name in errors:
      return errors[name]
    return sorted(errors.items())[0][1]  # first alphabetically

  def get_last_hidden_state(self):
    h = self.subnetwork.get_default_output_layer().get_last_hidden_state()
    if h is not None:
      return h
    return super(SubnetworkLayer, self).get_last_hidden_state()


class FramewiseStatisticsLayer(LayerBase):
  layer_class = "framewise_statistics"

  def __init__(self, sil_label_idx, histogram_num_bins=20, **kwargs):
    super(FramewiseStatisticsLayer, self).__init__(**kwargs)
    self.output.placeholder = tf.constant(0, name="dummy")
    assert self.sources, "give me some sources"
    # Currently, a bit hardcoded.
    # We expect a framewise hard alignment, and calculate FER, CE, perplexity,
    # for all frames, frames without silence, and silence frames.
    from TFUtil import flatten_with_seq_len_mask
    import numpy
    source = self.sources[0]
    output = source.output
    target = source._get_target_value()
    assert target.sparse
    assert source.output_before_activation.act_func is tf.nn.softmax
    output_seq_lens = output.size_placeholder[0]
    output_before_softmax_flat = flatten_with_seq_len_mask(source.output_before_activation.x, output_seq_lens, time_major=output.is_time_major)
    target_seq_lens = target.size_placeholder[0]
    target_flat = flatten_with_seq_len_mask(target.placeholder, target_seq_lens, time_major=target.is_time_major)
    target_flat.set_shape(tf.TensorShape([tf.Dimension(None)]))
    loss_ce = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=output_before_softmax_flat, labels=target_flat)
    flat_last_dim = output_before_softmax_flat.get_shape().ndims - 1
    assert flat_last_dim == 1
    output_flat = flatten_with_seq_len_mask(output.placeholder, output_seq_lens, time_major=output.is_time_major)
    output_flat_argmax = tf.cast(tf.arg_max(output_before_softmax_flat, dimension=flat_last_dim), "int32")
    frame_error = tf.not_equal(output_flat_argmax, target_flat)
    # target_flat is shape (time,) -> index.
    target_flat_exp = tf.stack([tf.range(tf.shape(target_flat)[0], dtype=tf.int32), target_flat], axis=1)
    true_label_prob = tf.gather_nd(output_flat, target_flat_exp)
    true_label_prob.set_shape(tf.TensorShape([tf.Dimension(None)]))
    true_label_prob_i32 = tf.clip_by_value(
      tf.cast(tf.round(true_label_prob * histogram_num_bins), tf.int32), 0, histogram_num_bins - 1)
    true_label_prob_histogram = tf.stack(
      [tf.equal(true_label_prob_i32, i) for i in range(histogram_num_bins)], axis=1)
    true_label_prob_histogram.set_shape(tf.TensorShape([tf.Dimension(None), tf.Dimension(histogram_num_bins)]))

    mask_no_sil = tf.not_equal(target_flat, sil_label_idx)
    mask_sil = tf.equal(target_flat, sil_label_idx)
    seq_len = tf.reduce_sum(target_seq_lens)
    seq_len_sil = tf.reduce_sum(tf.cast(mask_sil, tf.int32))
    seq_len_no_sil = tf.reduce_sum(tf.cast(mask_no_sil, tf.int32))

    with var_creation_scope():
      accumulated_seq_len = tf.Variable(initial_value=0, dtype=tf.int64, trainable=False, name="accumulated_seq_len")
      accumulated_seq_len_sil = tf.Variable(initial_value=0, dtype=tf.int64, trainable=False, name="accumulated_seq_len_sil")
    accumulated_seq_len = tf.assign_add(accumulated_seq_len, tf.cast(seq_len, tf.int64))
    accumulated_seq_len_sil = tf.assign_add(accumulated_seq_len_sil, tf.cast(seq_len_sil, tf.int64))
    accumulated_seq_len_no_sil = accumulated_seq_len - accumulated_seq_len_sil

    self.stats["batch_seq_length"] = seq_len
    self.stats["batch_seq_length_sil"] = seq_len_sil
    self.stats["batch_seq_length_no_sil"] = seq_len_no_sil
    self.stats["accumulated_seq_length"] = accumulated_seq_len
    self.stats["accumulated_seq_length_sil"] = accumulated_seq_len_sil
    self.stats["accumulated_seq_length_no_sil"] = accumulated_seq_len_no_sil

    for _k, _v in {
          "loss_ce": loss_ce,
          "frame_error": frame_error,
          "true_label_prob_histogram": true_label_prob_histogram}.items():
      for _k2 in ["", "_sil", "_no_sil"]:
        k = _k + _k2
        v = _v
        acc_seq_len = accumulated_seq_len
        if k.endswith("_no_sil"):
          v = tf.boolean_mask(v, mask_no_sil)
          acc_seq_len = accumulated_seq_len_no_sil
        elif k.endswith("_sil"):
          v = tf.boolean_mask(v, mask_sil)
          acc_seq_len = accumulated_seq_len_sil
        v_f32 = tf.cast(v, tf.float32)
        self.stats["batch_%s" % k] = tf.reduce_mean(v_f32, axis=0)
        if v.dtype.is_floating:
          acc_dtype = "float64"
        else:
          acc_dtype = "int64"
        acc_shape = v.get_shape().as_list()[1:]
        assert all(acc_shape)
        with var_creation_scope():
          acc_v = tf.Variable(initial_value=numpy.zeros(acc_shape, dtype=acc_dtype), dtype=acc_dtype, trainable=False, name="accumulated_%s" % k)
        acc_v = tf.assign_add(acc_v, tf.reduce_sum(tf.cast(v, acc_dtype), axis=0))
        self.stats["accumulated_%s" % k] = tf.cast(acc_v, tf.float64) / tf.cast(acc_seq_len, tf.float64)

    self.stats["batch_loss_perplexity"] = tf.exp(self.stats["batch_loss_ce"])
    self.stats["batch_loss_perplexity_sil"] = tf.exp(self.stats["batch_loss_ce_sil"])
    self.stats["batch_loss_perplexity_no_sil"] = tf.exp(self.stats["batch_loss_ce_no_sil"])
    self.stats["accumulated_loss_perplexity"] = tf.exp(self.stats["accumulated_loss_ce"])
    self.stats["accumulated_loss_perplexity_sil"] = tf.exp(self.stats["accumulated_loss_ce_sil"])
    self.stats["accumulated_loss_perplexity_no_sil"] = tf.exp(self.stats["accumulated_loss_ce_no_sil"])

  @classmethod
  def get_out_data_from_opts(cls, **kwargs):
    # n_out=1 is a workaround for now. Our output should not be used. We have none.
    return super(FramewiseStatisticsLayer, cls).get_out_data_from_opts(n_out=1, **kwargs)


class Loss(object):
  class_name = None
  recurrent = False  # if this is a frame-wise criteria, this will be False

  def __init__(self):
    # All are initialized in self.init().
    self.output = None  # type: Data
    self.time_major = None  # type: bool|None
    self.output_with_activation = None  # type: OutputWithActivation
    self.output_seq_lens = None  # type: tf.Tensor
    self.target = None  # type: Data
    self.target_seq_lens = None  # type: tf.Tensor
    self.output_flat = None  # type: tf.Tensor
    self.output_before_softmax_flat = None  # type: tf.Tensor
    self.target_flat = None  # type: tf.Tensor
    # Maybe make configurable. For now, same as in our Theano behavior.
    self.reduce_func = tf.reduce_sum  # or tf.reduce_mean

  def init(self, output, output_with_activation=None, target=None):
    """
    :param Data output: generated output
    :param OutputWithActivation|None output_with_activation:
    :param Data target: reference target from dataset
    """
    from TFUtil import flatten_with_seq_len_mask
    with tf.name_scope("loss_init"):
      self.output = output
      self.output_with_activation = output_with_activation
      self.output_seq_lens = output.size_placeholder[0]
      self.target = target
      self.target_seq_lens = target.size_placeholder[0]
      # Flat variants are with batch,time collapsed into one, masked via seq_lens.
      self.output_flat = None
      self.output_before_softmax_flat = None
      if output_with_activation:
        assert output_with_activation.y is output.placeholder
      if self.output.have_tim_axis():
        time_and_batch_dims = (self.output.time_dim_axis, self.output.batch_dim_axis)
        assert time_and_batch_dims in [(0, 1), (1, 0)], "output time-batch-dim unexpected: %s" % self.output
        if output_with_activation and output_with_activation.act_func is tf.nn.softmax:
          self.output_before_softmax_flat = flatten_with_seq_len_mask(output_with_activation.x, self.output_seq_lens, time_major=output.is_time_major)
        else:
          self.output_flat = flatten_with_seq_len_mask(output.placeholder, self.output_seq_lens, time_major=output.is_time_major)
      self.target_flat = flatten_with_seq_len_mask(target.placeholder, self.target_seq_lens, time_major=target.is_time_major)
      self._check_init()

  def _check_init(self):
    """
    Does some checks on self.target and self.output, e.g. if the dense shapes matches.
    You can overwrite this if those checks don't make sense for your derived loss class.
    """
    assert self.target.ndim_dense == self.output.ndim_dense, (
      "Number of dimensions missmatch. Target: %s, output: %s" % (self.target, self.output))
    expected_output_dim = self.get_auto_output_layer_dim(self.target.dim)
    assert expected_output_dim == self.output.dim, (
      "Expected output dim is %i but the output has dim %i. " % (expected_output_dim, self.output.dim) +
      "Target: %s, output: %s" % (self.target, self.output))

  def get_error(self):
    """
    :return: frame error rate as a scalar value
    :rtype: tf.Tensor
    """
    with tf.name_scope("loss_frame_error"):
      assert self.output.ndim_dense == self.target.ndim_dense
      from TFUtil import check_input_ndim, check_shape_equal
      output_flat = self.output_before_softmax_flat
      if output_flat is None:
        output_flat = self.output_flat
      output_flat = check_input_ndim(output_flat, ndim=2)
      last_dim = tf.rank(output_flat) - 1  # should be 1
      if self.target.sparse:
        target_label = check_input_ndim(self.target_flat, ndim=1)
      else:
        target_flat = check_shape_equal(self.target_flat, output_flat)
        target_label = tf.cast(tf.arg_max(target_flat, dimension=last_dim), tf.int32)
      output_label = tf.cast(tf.arg_max(output_flat, dimension=last_dim), target_label.dtype)
      not_equal = tf.not_equal(output_label, target_label)
      return self.reduce_func(tf.cast(not_equal, tf.float32))

  def get_value(self):
    """
    :return: loss as a scalar value
    :rtype: tf.Tensor
    """
    raise NotImplementedError

  def get_auto_output_layer_dim(self, target_dim):
    """
    :param int target_dim:
    :return: normally just the same as target_dim. e.g. for CTC, we would add 1 for the blank label
    :rtype: int
    """
    return target_dim


class CrossEntropyLoss(Loss):
  class_name = "ce"

  def get_value(self):
    with tf.name_scope("loss_ce"):
      assert self.target.ndim_dense == self.output.ndim_dense
      if self.target.sparse:
        if self.output_before_softmax_flat is not None:
          out = tf.nn.sparse_softmax_cross_entropy_with_logits(
            logits=self.output_before_softmax_flat, labels=self.target_flat)
          return self.reduce_func(out)
        else:
          target_flat_exp = tf.stack(
            [tf.range(tf.shape(self.target_flat)[0], dtype=tf.int32),
             tf.cast(self.target_flat, tf.int32)], axis=1)  # (time,2)
          out = tf.log(tf.gather_nd(self.output_flat, target_flat_exp))
          return -self.reduce_func(out)
      else:  # not sparse
        if self.output_before_softmax_flat is not None:
          out = tf.nn.softmax_cross_entropy_with_logits(self.output_before_softmax_flat, self.target_flat)
          return self.reduce_func(out)
        else:
          out = self.target_flat * tf.log(self.output_flat)
          return -self.reduce_func(out)


class GenericCELoss(Loss):
  class_name = "generic_ce"

  def __init__(self, **kwargs):
    super(GenericCELoss, self).__init__(**kwargs)

    def loss(z, y, grad_f, target):
      nlog_scores = -tf.log(tf.clip_by_value(y, 1.e-20, 1.e20))  # (time,dim)
      # target is shape (time,) -> index.
      target_exp = tf.stack([tf.range(tf.shape(target)[0], dtype=tf.int32), target], axis=1)  # (time,2)
      # Thus K == 2. gather_nd out will be (target_exp.shape[0],) = (time,).
      gathered = tf.gather_nd(nlog_scores, target_exp)   # (time,)
      return self.reduce_func(gathered)

    def loss_grad(op, grad):
      """
      :param tf.Operation op:
      :param tf.Tensor grad: grad for loss
      :return: grad for op.inputs
      """
      z, y, grad_f, target = op.inputs
      num_classes = tf.shape(z)[-1]
      bw = tf.one_hot(target, depth=num_classes)
      grad_z = grad_f * (y - bw)
      return grad_z, None, None, None  # for each input

    # We need to create the loss func here in __init__ to register it in the default graph as early as possible,
    # before we create the TF session.
    from TFUtil import custom_gradient
    self._loss_func = custom_gradient.register(
      [tf.float32, tf.float32, tf.float32, tf.int32], op=loss, grad_op=loss_grad)

  def get_value(self):
    # Should be generic for any activation function.
    # (Except when the labels are not independent, such as for softmax.)
    # See Theano NetworkOutputLayer.FramewiseOutputLayer.cost() with "generic_ce" loss.
    from TFUtil import flatten_with_seq_len_mask
    # activation function can be anything, e.g. exp or sigmoid, but not softmax, must be elemwise.
    assert self.output_with_activation
    x = self.output_with_activation.x
    y = self.output_with_activation.y
    grad_f, = tf.gradients(tf.log(y), x)
    assert grad_f is not None
    grad_f = flatten_with_seq_len_mask(grad_f, seq_lens=self.output_seq_lens, time_major=self.output.is_time_major)
    x = flatten_with_seq_len_mask(x, seq_lens=self.output_seq_lens, time_major=self.output.is_time_major)
    y = flatten_with_seq_len_mask(y, seq_lens=self.output_seq_lens, time_major=self.output.is_time_major)
    assert y.get_shape().ndims == 2
    y /= tf.reduce_sum(y, axis=1, keep_dims=True)
    assert self.output.dim == self.target.dim
    assert self.target.sparse
    return self._loss_func(x, y, grad_f, self.target_flat)


class CtcLoss(Loss):
  class_name = "ctc"
  recurrent = True

  def __init__(self, target_collapse_repeated=False, auto_clip_target_len=False):
    """
    :param bool target_collapse_repeated: like preprocess_collapse_repeated option for CTC. used for sparse_labels().
    :param bool auto_clip_target_len: see self._get_target_sparse_labels().
    """
    super(CtcLoss, self).__init__()
    self.target_collapse_repeated = target_collapse_repeated
    self.auto_clip_target_len = auto_clip_target_len
    self._target_sparse_labels = None

  def init(self, **kwargs):
    self._target_sparse_labels = None
    super(CtcLoss, self).init(**kwargs)

  def _get_target_sparse_labels(self):
    if self._target_sparse_labels is not None:
      return self._target_sparse_labels
    from TFUtil import sparse_labels
    target_seq_lens = self.target_seq_lens
    if self.auto_clip_target_len:
      # Not more than output_seq_lens, otherwise we can get an exception by the CTC algorithm
      # "Not enough time for target transition sequence".
      # One less to allow for at least one blank somewhere.
      target_seq_lens = tf.minimum(target_seq_lens, tf.maximum(self.output_seq_lens - 1, 0))
    labels = sparse_labels(self.target.placeholder, target_seq_lens,
                           collapse_repeated=self.target_collapse_repeated)
    self._target_sparse_labels = labels
    return labels

  def get_value(self):
    if not self.target.sparse:
      raise Exception("CTC target expected to be sparse (symbols)")
    with tf.name_scope("loss_ctc"):
      logits = self.output_with_activation
      if self.output_with_activation:
        logits = self.output_with_activation.get_logits()
      if logits is None:
        logits = tf.log(self.output.placeholder)
      assert logits.get_shape().ndims == 3  # (B,T,N) or (T,B,N)
      assert logits.get_shape().dims[2].value == self.target.dim + 1  # one more for blank
      seq_lens = self.output_seq_lens
      labels = self._get_target_sparse_labels()
      loss = tf.nn.ctc_loss(inputs=logits, labels=labels, sequence_length=seq_lens, time_major=self.output.is_time_major)
      return self.reduce_func(loss)

  def get_error(self):
    if not self.target.sparse:
      raise Exception("CTC target expected to be sparse (symbols)")
    with tf.name_scope("loss_ctc_error"):
      logits = None
      if self.output_with_activation:
        logits = self.output_with_activation.get_logits()
      if logits is None:
        logits = tf.log(self.output.placeholder)
      if not self.output.is_time_major:
        logits = tf.transpose(logits, [1, 0, 2])  # (B,T,N) => (T,B,N)
      seq_lens = self.output_seq_lens
      decoded, _ = tf.nn.ctc_greedy_decoder(inputs=logits, sequence_length=seq_lens)
      labels = self._get_target_sparse_labels()
      error = tf.edit_distance(hypothesis=tf.cast(decoded[0], labels.dtype), truth=labels, normalize=False)
      return self.reduce_func(error)

  def get_auto_output_layer_dim(self, target_dim):
    return target_dim + 1  # one added for blank


_LossClassDict = {}  # type: dict[str,type(Loss)]

def get_loss_class(loss):
  """
  :param str loss: loss type such as "ce"
  :rtype: () -> Loss
  """
  if not _LossClassDict:
    for v in globals().values():
      if isinstance(v, type) and issubclass(v, Loss) and v.class_name:
        assert v.class_name not in _LossClassDict
        _LossClassDict[v.class_name] = v
  return _LossClassDict[loss]


_LayerClassDict = {}  # type: dict[str,type(LayerBase)]

def _init_layer_class_dict():
  for v in globals().values():
    if isinstance(v, type) and issubclass(v, LayerBase) and v.layer_class:
      assert v.layer_class not in _LayerClassDict
      _LayerClassDict[v.layer_class] = v
  for alias, v in {"forward": LinearLayer}.items():
    assert alias not in _LayerClassDict
    _LayerClassDict[alias] = v


def get_layer_class(name):
  """
  :param str name: matches layer_class
  :rtype: (() -> LayerBase) | LayerBase
  """
  if not _LayerClassDict:
    _init_layer_class_dict()
  if name not in _LayerClassDict:
    raise Exception("unknown layer class %r" % name)
  return _LayerClassDict[name]

