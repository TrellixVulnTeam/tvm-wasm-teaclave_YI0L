# pylint: disable=invalid-name, import-self
"""Keras frontend."""
from __future__ import absolute_import as _abs
import sys
import numpy as np
from .. import ir_pass
from .. import expr as _expr
from .. import op as _op
from ... import nd as _nd
from .common import ExprTable

__all__ = ['from_keras']


def _check_data_format(keras_layer):
    if hasattr(keras_layer, ('data_format')):
        if keras_layer.data_format != 'channels_last':
            raise ValueError("Keras frontend currently supports data_format = channels_last only.")


def _get_pad_pair(input1d, kernel1d, stride1d):
    out1d = (input1d + stride1d - 1) // stride1d
    pad = np.maximum((out1d - 1) * stride1d + kernel1d - input1d, 0)
    pad_before = pad // 2
    pad_after = pad - pad_before
    return [pad_before, pad_after]


def _get_elu(inexpr, alpha):
    """A helper method for elu."""
    return _op.negative(alpha) * _op.nn.relu(_expr.const(1.) - \
        _op.exp(inexpr)) + _op.nn.relu(inexpr)


def _as_list(arr):
    """Force being a list, ignore if already is."""
    if isinstance(arr, list):
        return arr
    return [arr]


def _convert_recurrent_activation(inexpr, keras_layer):
    act_type = keras_layer.recurrent_activation.__name__
    return _convert_activation(inexpr, act_type, None)


def _convert_activation(inexpr, keras_layer, _):
    if isinstance(keras_layer, str):
        act_type = keras_layer
    else:
        if sys.version_info.major < 3:
            act_type = keras_layer.activation.func_name
        else:
            act_type = keras_layer.activation.__name__
    if act_type == 'linear':
        if isinstance(keras_layer, str):
            return inexpr
        alpha = keras_layer.alpha if hasattr(keras_layer, 'alpha') else 1.
        beta = keras_layer.beta if hasattr(keras_layer, 'beta') else 0.
        alpha = _expr.const(alpha, dtype='float32')
        beta = _expr.const(beta, dtype='float32')
        return _op.add(_op.multiply(inexpr, alpha), beta)
    elif act_type == 'softmax':
        return _op.nn.softmax(inexpr, axis=1)
    elif act_type == 'sigmoid':
        return _op.sigmoid(inexpr)
    elif act_type == 'tanh':
        return _op.tanh(inexpr)
    elif act_type == 'relu':
        return _op.nn.relu(inexpr)
    elif act_type == 'softplus':
        return _op.log(_op.add(_op.exp(inexpr), _expr.const(1.)))
    elif act_type == 'elu':
        alpha = keras_layer.alpha if hasattr(keras_layer, 'alpha') else 1.
        alpha = _expr.const(alpha, dtype='float32')
        return _get_elu(inexpr, alpha)
    elif act_type == 'selu':
        # Alpha, Gamma values obtained from https://arxiv.org/abs/1706.02515
        alpha = keras_layer.alpha if hasattr(keras_layer, 'alpha') \
            else 1.6732632423543772848170429916717
        gamma = keras_layer.gamma if hasattr(keras_layer, 'gamma') \
            else 1.0507009873554804934193349852946
        alpha = _expr.const(alpha, dtype='float32')
        gamma = _expr.const(gamma, dtype='float32')
        return gamma * _get_elu(inexpr, alpha)
    elif act_type == 'relu6':
        return _op.clip(inexpr, a_min=0., a_max=6.)
    elif act_type == 'softsign':
        return inexpr / (_expr.const(1.) + _op.abs(inexpr))
    elif act_type == 'hard_sigmoid':
        transformX = (_expr.const(0.2) * inexpr) + _expr.const(0.5)
        return _op.clip(transformX, a_min=0., a_max=1.)
    else:
        raise TypeError("Unsupported activation type : {}".format(act_type))


def _convert_advanced_activation(inexpr, keras_layer, etab):
    act_type = type(keras_layer).__name__
    if act_type == 'ReLU':
        if keras_layer.max_value:
            return _op.clip(inexpr, a_min=0., a_max=float(keras_layer.max_value))
        return _op.nn.relu(inexpr)
    elif act_type == 'LeakyReLU':
        return _op.nn.leaky_relu(inexpr, alpha=float(keras_layer.alpha))
    elif act_type == 'ELU':
        alpha = keras_layer.alpha if hasattr(keras_layer, 'alpha') else 1.
        alpha = _expr.const(alpha, dtype='float32')
        return _get_elu(inexpr, alpha)
    elif act_type == 'PReLU':
        assert hasattr(keras_layer, 'alpha'), "alpha required for PReLU."
        _check_data_format(keras_layer)
        size = len(keras_layer.alpha.shape)
        alpha = etab.new_const(keras_layer.get_weights()[0] \
                               .transpose(np.roll(range(size), 1)))
        return _op.negative(alpha) * _op.nn.relu(_op.negative(inexpr)) + _op.nn.relu(inexpr)
    elif act_type == 'ThresholdedReLU':
        theta = keras_layer.theta if hasattr(keras_layer, 'theta') else 1.
        return _op.multiply(inexpr, _op.greater(inexpr, \
            _expr.const(theta, dtype='float32')).astype('float32'))
    else:
        raise TypeError("Unsupported advanced activation type : {}".format(act_type))


def _convert_merge(inexpr, keras_layer, _):
    merge_type = type(keras_layer).__name__
    ret = inexpr[0]
    if merge_type == 'Subtract':
        assert len(inexpr) == 2, "Subtract merge takes 2 inputs."
        ret = _op.subtract(ret, inexpr[1])
    elif merge_type in ['Add', 'Multiply', 'Maximum']:
        op_map = {'Add':_op.add, 'Multiply':_op.multiply, 'Maximum':_op.maximum}
        for i in range(1, len(inexpr)):
            ret = op_map[merge_type](ret, inexpr[i])
    elif merge_type == 'Average':
        for i in range(1, len(inexpr)):
            ret = _op.add(ret, inexpr[i])
        ret = ret / _expr.const(len(inexpr), dtype='float32')
    else:
        raise TypeError("Unsupported merge type : {}".format(merge_type))
    return ret


def _convert_dense(inexpr, keras_layer, etab):
    weightList = keras_layer.get_weights()
    weight = etab.new_const(weightList[0].transpose([1, 0]))
    params = {'weight':weight, 'units':weightList[0].shape[1]}
    input_shape = keras_layer.input_shape
    input_dim = len(input_shape)
    # In case of RNN dense, input shape will be (1, 1, n)
    if input_dim > 2:
        input_shape = tuple(dim if dim else 1 for dim in _as_list(input_shape)[0])
        if input_dim != 3 or input_shape[0] != 1 or input_shape[1] != 1:
            raise ValueError("Cannot flatten the inputs with shape.", input_shape, " for dense.")
        inexpr = _op.squeeze(inexpr, axis=0)
    out = _op.nn.dense(data=inexpr, **params)
    if keras_layer.use_bias:
        bias = etab.new_const(weightList[1])
        out = _op.nn.bias_add(out, bias)
    # defuse activation
    if sys.version_info.major < 3:
        act_type = keras_layer.activation.func_name
    else:
        act_type = keras_layer.activation.__name__
    if act_type != 'linear':
        out = _convert_activation(out, act_type, etab)
    if input_dim > 2:
        out = _op.expand_dims(out, axis=0)
    return out


def _convert_convolution(inexpr, keras_layer, etab):
    _check_data_format(keras_layer)
    is_deconv = type(keras_layer).__name__ == 'Conv2DTranspose'
    is_depthconv = type(keras_layer).__name__ == 'DepthwiseConv2D'
    weightList = keras_layer.get_weights()
    if is_deconv:
        kernel_h, kernel_w, n_filters, in_channels = weightList[0].shape
        weight = weightList[0].transpose([3, 2, 0, 1])
    elif is_depthconv:
        kernel_h, kernel_w, in_channels, depth_mult = weightList[0].shape
        weight = weightList[0].transpose([2, 3, 0, 1])
    else:
        kernel_h, kernel_w, in_channels, n_filters = weightList[0].shape
        weight = weightList[0].transpose([3, 2, 0, 1])
    dilation = [1, 1]
    if isinstance(keras_layer.dilation_rate, (list, tuple)):
        dilation = [keras_layer.dilation_rate[0], keras_layer.dilation_rate[1]]
    else:
        dilation = [keras_layer.dilation_rate, keras_layer.dilation_rate]
    dilated_kernel_h = (kernel_h - 1) * dilation[0] + 1
    dilated_kernel_w = (kernel_w - 1) * dilation[1] + 1
    stride_h, stride_w = keras_layer.strides
    params = {'weight': etab.new_const(weight),
              'kernel_size': [kernel_h, kernel_w],
              'strides': [stride_h, stride_w],
              'dilation': dilation,
              'padding': [0, 0]}
    if is_depthconv:
        params['channels'] = in_channels * depth_mult
        params['groups'] = in_channels
    else:
        params['channels'] = n_filters
    if keras_layer.padding == 'valid':
        pass
    # we insert a separate pad operator
    elif keras_layer.padding == 'same':
        in_h = keras_layer.input_shape[1]
        in_w = keras_layer.input_shape[2]
        pad_t, pad_b = _get_pad_pair(in_h, dilated_kernel_h, stride_h)
        pad_l, pad_r = _get_pad_pair(in_w, dilated_kernel_w, stride_w)
        if pad_t == pad_b and pad_l == pad_r:
            params['padding'] = (pad_t, pad_l)
        else:
            inexpr = _op.nn.pad(data=inexpr, pad_width=(
                (0, 0), (0, 0), (pad_t, pad_b), (pad_l, pad_r)))
    else:
        raise TypeError("Unsupported padding type : {}".format(keras_layer.padding))
    if is_deconv:
        out = _op.nn.conv2d_transpose(data=inexpr, **params)
    else:
        out = _op.nn.conv2d(data=inexpr, **params)
    if keras_layer.use_bias:
        bias = etab.new_const(weightList[1])
        out = _op.nn.bias_add(out, bias)
    # defuse activation
    if sys.version_info.major < 3:
        act_type = keras_layer.activation.func_name
    else:
        act_type = keras_layer.activation.__name__
    if act_type != 'linear':
        out = _convert_activation(out, act_type, etab)
    return out


def _convert_separable_convolution(inexpr, keras_layer, etab):
    _check_data_format(keras_layer)
    weightList = keras_layer.get_weights()
    # depthwise conv
    kernel_h, kernel_w, in_channels, depth_mult = weightList[0].shape
    stride_h, stride_w = keras_layer.strides
    weight0 = weightList[0].transpose([2, 3, 0, 1])
    params0 = {'weight': etab.new_const(weight0),
               'channels': in_channels * depth_mult,
               'groups': in_channels,
               'kernel_size': [kernel_h, kernel_w],
               'strides': [stride_h, stride_w],
               'dilation': [1, 1],
               'padding': [0, 0]}
    if keras_layer.padding == 'valid':
        pass
    # we insert a separate pad operator
    elif keras_layer.padding == 'same':
        in_h = keras_layer.input_shape[1]
        in_w = keras_layer.input_shape[2]
        pad_t, pad_b = _get_pad_pair(in_h, kernel_h, stride_h)
        pad_l, pad_r = _get_pad_pair(in_w, kernel_w, stride_w)
        if pad_t == pad_b and pad_l == pad_r:
            params0['padding'] = (pad_t, pad_l)
        else:
            inexpr = _op.nn.pad(data=inexpr, pad_width=(
                (0, 0), (0, 0), (pad_t, pad_b), (pad_l, pad_r)))
    else:
        raise TypeError("Unsupported padding type : {}".format(keras_layer.padding))
    depthconv = _op.nn.conv2d(data=inexpr, **params0)
    # pointwise conv
    weight1 = weightList[1].transpose([3, 2, 0, 1])
    params1 = {'weight': etab.new_const(weight1),
               'channels': weight1.shape[0],
               'groups': 1,
               'kernel_size': [1, 1],
               'strides': [1, 1],
               'dilation': [1, 1]}
    out = _op.nn.conv2d(data=depthconv, **params1)
    if keras_layer.use_bias:
        bias = etab.new_const(weightList[2])
        out = _op.nn.bias_add(out, bias)
    # defuse activation
    if sys.version_info.major < 3:
        act_type = keras_layer.activation.func_name
    else:
        act_type = keras_layer.activation.__name__
    if act_type != 'linear':
        out = _convert_activation(out, act_type, etab)
    return out


def _convert_flatten(inexpr, keras_layer, _):
    _check_data_format(keras_layer)
    # NCHW -> NHWC so that dense can be correctly converted
    inexpr = _op.transpose(inexpr, axes=[0, 2, 3, 1])
    return _op.nn.batch_flatten(inexpr)


def _convert_pooling(inexpr, keras_layer, etab):
    _check_data_format(keras_layer)
    pool_type = type(keras_layer).__name__
    # global pool in keras = global pool + flatten in nnvm/relay
    if pool_type == 'GlobalMaxPooling2D':
        return _convert_flatten(_op.nn.global_max_pool2d(inexpr), keras_layer, etab)
    elif pool_type == 'GlobalAveragePooling2D':
        return _convert_flatten(_op.nn.global_avg_pool2d(inexpr), keras_layer, etab)
    else:
        pool_h, pool_w = keras_layer.pool_size
        stride_h, stride_w = keras_layer.strides
        params = {'pool_size': [pool_h, pool_w],
                  'strides': [stride_h, stride_w],
                  'padding': [0, 0]}
        if keras_layer.padding == 'valid':
            pass
        elif keras_layer.padding == 'same':
            in_h = keras_layer.input_shape[1]
            in_w = keras_layer.input_shape[2]
            pad_t, pad_b = _get_pad_pair(in_h, pool_h, stride_h)
            pad_l, pad_r = _get_pad_pair(in_w, pool_w, stride_w)
            params['padding'] = [pad_t, pad_l, pad_b, pad_r]
        else:
            raise TypeError("Unsupported padding type : {}".format(keras_layer.padding))
        if pool_type == 'MaxPooling2D':
            return _op.nn.max_pool2d(inexpr, **params)
        elif pool_type == 'AveragePooling2D':
            params['count_include_pad'] = False
            return _op.nn.avg_pool2d(inexpr, **params)
        else:
            raise TypeError("Unsupported pooling type : {}".format(keras_layer))


def _convert_upsample(inexpr, keras_layer, _):
    _check_data_format(keras_layer)
    upsample_type = type(keras_layer).__name__
    if upsample_type == 'UpSampling1D':
        h = keras_layer.size
        params = {'scale': h}
    elif upsample_type == 'UpSampling2D':
        h, w = keras_layer.size
        if h != w:
            raise TypeError("Unsupported upsampling type with different axes size : {}"
                            .format(keras_layer.size))
        params = {'scale': h}
    elif upsample_type == 'UpSampling3D':
        h, w, d = keras_layer.size
        if h != w or w != d:
            raise TypeError("Unsupported upsampling type with different axes size : {}"
                            .format(keras_layer.size))
        params = {'scale': h}
    else:
        raise TypeError("Unsupported upsampling type : {}".format(upsample_type))
    return _op.nn.upsampling(inexpr, **params)


def _convert_cropping(inexpr, keras_layer, _):
    _check_data_format(keras_layer)
    crop_type = type(keras_layer).__name__
    if crop_type == 'Cropping1D':
        raise NotImplementedError("Cropping1D not implemented")
    elif crop_type == 'Cropping2D':
        (_, in_h, in_w, _) = keras_layer.input_shape
        ((crop_t, crop_b), (crop_l, crop_r)) = keras_layer.cropping
    else:
        raise TypeError("Unrecognized cropping type : {}".format(crop_type))
    int32_max = np.iinfo(np.int32).max
    return _op.strided_slice(inexpr, begin=[0, 0, crop_t, crop_l], \
        end=[int32_max, int32_max, in_h-crop_b, in_w-crop_r])


def _convert_batchnorm(inexpr, keras_layer, etab):
    params = {'scale': False,
              'center': False,
              'epsilon': keras_layer.epsilon}
    idx = 0
    if keras_layer.scale:
        params['scale'] = True
        gamma = keras_layer.get_weights()[idx]
        params['gamma'] = etab.new_const(gamma)
        idx += 1
    if keras_layer.center:
        params['center'] = True
        beta = keras_layer.get_weights()[idx]
        params['beta'] = etab.new_const(beta)
        idx += 1
    moving_mean = keras_layer.get_weights()[idx]
    moving_var = keras_layer.get_weights()[idx + 1]
    params['moving_mean'] = etab.new_const(moving_mean)
    params['moving_var'] = etab.new_const(moving_var)
    result, moving_mean, moving_var = _op.nn.batch_norm(inexpr, **params)
    return result


def _convert_padding(inexpr, keras_layer, _):
    _check_data_format(keras_layer)
    padding_type = type(keras_layer).__name__
    padding = keras_layer.padding
    top = left = bottom = right = 0
    if padding_type == 'ZeroPadding2D':
        if isinstance(padding, int):
            top = left = bottom = right = padding
        elif isinstance(padding, tuple):
            if isinstance(padding[0], int):
                top, left = padding
                bottom, right = padding
            elif isinstance(padding[0], tuple):
                top, bottom = padding[0]
                left, right = padding[1]
            else:
                raise ValueError("Unrecognized padding option: {}".format(str(padding)))
        else:
            raise ValueError("Unrecognized padding option: {}".format(str(padding)))
    elif padding_type == 'ZeroPadding1D':
        raise NotImplementedError("ZeroPadding1D not implemented")
    else:
        raise ValueError("Unrecognized padding type: {}".format(padding_type))
    return _op.nn.pad(data=inexpr, pad_width=((0, 0), (0, 0), (top, bottom), (left, right)))


def _convert_concat(inexpr, keras_layer, _):
    _check_data_format(keras_layer)
    return _op.concatenate(_as_list(inexpr), axis=1)


def _convert_reshape(inexpr, keras_layer, _):
    _check_data_format(keras_layer)
    ch = keras_layer.input_shape[-1]
    assert ch == keras_layer.target_shape[-1], \
        "Only supports last dimension in target shape being equal to " \
        "the channel number of input tensor."
    shape = (-1, ch) + keras_layer.target_shape[:-1]
    return _op.reshape(inexpr, newshape=shape)


def _convert_lstm(inexpr, keras_layer, etab):
    _check_data_format(keras_layer)
    if not isinstance(inexpr, list):
        buf = np.zeros((1, keras_layer.units), 'float32')
        c_op = etab.new_const(buf)
        h_op = etab.new_const(buf)
        inexpr = [inexpr, h_op, c_op]
    in_data = inexpr[0]
    next_h = inexpr[1]
    next_c = inexpr[2]
    weightList = keras_layer.get_weights()
    in_shape = tuple(dim if dim else 1 for dim in _as_list(keras_layer.input_shape)[0])
    kernel_weight = etab.new_const(weightList[0].transpose([1, 0]))
    recurrent_weight = etab.new_const(weightList[1].transpose([1, 0]))
    in_bias = etab.new_const(weightList[2])
    units = list(weightList[0].shape)[1]
    time_steps = in_shape[1]
    in_data = _op.squeeze(in_data, axis=[0])
    in_data = _op.split(in_data, indices_or_sections=time_steps, axis=0)
    # loop for the number of time_steps
    for data in in_data:
        ixh1 = _op.nn.dense(data, kernel_weight, units=units)
        ixh2 = _op.nn.bias_add(_op.nn.dense(next_h, recurrent_weight, units=units), bias=in_bias)
        gate = ixh1 + ixh2
        gates = _op.split(gate, indices_or_sections=4, axis=1)
        in_gate = _convert_recurrent_activation(gates[0], keras_layer)
        in_transform = _convert_recurrent_activation(gates[1], keras_layer)
        next_c = in_transform * next_c + in_gate * _convert_activation(gates[2], keras_layer, None)
        out_gate = _convert_recurrent_activation(gates[3], keras_layer)
        next_h = out_gate * _convert_activation(next_c, keras_layer, None)
    out_shape = tuple(dim if dim else 1 for dim in _as_list(keras_layer.output_shape)[0])
    out = _op.reshape(next_h, newshape=out_shape)
    return [out, next_h, next_c]


def _convert_simple_rnn(inexpr, keras_layer, etab):
    _check_data_format(keras_layer)
    if not isinstance(inexpr, list):
        buf = np.zeros((1, keras_layer.units), 'float32')
        prev_op = etab.new_const(buf)
        inexpr = [inexpr, prev_op]
    in_data = inexpr[0]
    prev_op = inexpr[1]
    weightList = keras_layer.get_weights()
    kernel_weight = etab.new_const(weightList[0].transpose([1, 0]))
    recurrent_weight = etab.new_const(weightList[1].transpose([1, 0]))
    in_bias = etab.new_const(weightList[2])
    units = list(weightList[0].shape)[1]
    in_data = _op.nn.batch_flatten(in_data)
    ixh = _op.nn.bias_add(_op.nn.dense(in_data, kernel_weight, units=units), bias=in_bias)
    prev_op = _op.nn.batch_flatten(prev_op)
    ixh2 = _op.nn.dense(prev_op, recurrent_weight, units=units)
    output = ixh + ixh2
    output = _convert_activation(output, keras_layer, None)
    out_shape = tuple(dim if dim else 1 for dim in _as_list(keras_layer.output_shape)[0])
    output = _op.reshape(output, newshape=out_shape)
    return [output, output]


def _convert_gru(inexpr, keras_layer, etab):
    _check_data_format(keras_layer)
    if not isinstance(inexpr, list):
        buf = np.zeros((1, keras_layer.units), 'float32')
        h_tm1 = etab.new_const(buf)
        inexpr = [inexpr, h_tm1]
    in_data = inexpr[0]
    h_tm1_op = inexpr[1]
    weightList = keras_layer.get_weights()
    kernel_weight = etab.new_const(weightList[0].transpose([1, 0]))
    recurrent_weight = etab.new_const(weightList[1].transpose([1, 0]))
    in_bias = etab.new_const(weightList[2])
    units = list(weightList[0].shape)[1]
    in_data = _op.nn.batch_flatten(in_data)
    matrix_x = _op.nn.bias_add(_op.nn.dense(in_data, kernel_weight, units=units), in_bias)
    # inputs projected by all gate matrices at once
    split_indices = [keras_layer.units, 2 * keras_layer.units]
    gates = _op.split(matrix_x, indices_or_sections=split_indices, axis=1)
    x_z = gates[0]
    x_r = gates[1]
    x_h = gates[2]
    # hidden state projected separately for update/reset and new
    units = 2 * keras_layer.units
    split_indices = [units]
    rec_weights = _op.split(recurrent_weight, indices_or_sections=split_indices, axis=0)
    h_tm1_op = _op.nn.batch_flatten(h_tm1_op)
    matrix_inner = _op.nn.dense(h_tm1_op, rec_weights[0], units=units)
    split_indices = [keras_layer.units]
    recurrent = _op.split(matrix_inner, indices_or_sections=split_indices, axis=1)
    recurrent_z = recurrent[0]
    recurrent_r = recurrent[1]
    rec_act_z = _convert_recurrent_activation(x_z + recurrent_z, keras_layer)
    rec_act_r = _convert_recurrent_activation(x_r + recurrent_r, keras_layer)
    units = keras_layer.units
    recurrent_h = _op.nn.dense(rec_act_r * h_tm1_op, rec_weights[1], units=units)
    act_hh = _convert_activation(x_h + recurrent_h, keras_layer, None)
    # previous and candidate state mixed by update gate
    output = rec_act_z * h_tm1_op + (_expr.const(1.) - rec_act_z) * act_hh
    out_shape = tuple(dim if dim else 1 for dim in _as_list(keras_layer.output_shape)[0])
    output = _op.reshape(output, newshape=out_shape)
    return [output, output]


def _default_skip(inexpr, keras_layer, _): # pylint: disable=unused-argument
    """Layers that can be skipped because they are train time only."""
    return inexpr


_convert_map = {
    'Dense'                    : _convert_dense,
    'Activation'               : _convert_activation,
    'ReLU'                     : _convert_advanced_activation,
    'LeakyReLU'                : _convert_advanced_activation,
    'PReLU'                    : _convert_advanced_activation,
    'ELU'                      : _convert_advanced_activation,
    'ThresholdedReLU'          : _convert_advanced_activation,

    'AveragePooling2D'         : _convert_pooling,
    'MaxPooling2D'             : _convert_pooling,
    'GlobalAveragePooling2D'   : _convert_pooling,
    'GlobalMaxPooling2D'       : _convert_pooling,
    'Conv2D'                   : _convert_convolution,
    'Conv2DTranspose'          : _convert_convolution,
    'DepthwiseConv2D'          : _convert_convolution,
    'SeparableConv2D'          : _convert_separable_convolution,

    'Flatten'                  : _convert_flatten,
    'Reshape'                  : _convert_reshape,
    'Concatenate'              : _convert_concat,
    'BatchNormalization'       : _convert_batchnorm,

    'Add'                      : _convert_merge,
    'Subtract'                 : _convert_merge,
    'Multiply'                 : _convert_merge,
    'ZeroPadding2D'            : _convert_padding,
    'UpSampling2D'             : _convert_upsample,
    'Cropping2D'               : _convert_cropping,

    # 'ZeroPadding1D'          : _convert_padding,
    # 'AveragePooling1D'       : _convert_pooling,
    # 'MaxPooling1D'           : _convert_pooling,
    # 'GlobalAveragePooling1D' : _convert_pooling,
    # 'GlobalMaxPooling1D'     : _convert_pooling,
    # 'Cropping1D'             : _convert_cropping,
    # 'UpSampling1D'           : _convert_upsample,
    # 'UpSampling3D'           : _convert_upsample,
    # 'Conv1D'                 : _convert_convolution1d,

    'SimpleRNN'                : _convert_simple_rnn,
    'LSTM'                     : _convert_lstm,
    'GRU'                      : _convert_gru,
    # 'Bidirectional'          : _convert_bidirectional,
    # 'TimeDistributed'        : _default_skip,

    'Average'                : _convert_merge,
    'Maximum'                : _convert_merge,
    # 'Dot'                    : _convert_merge,
    # 'Permute'                : _convert_permute,
    # 'Embedding'              : _convert_embedding,
    # 'RepeatVector'           : _convert_repeat_vector,

    'InputLayer'               : _default_skip,
    'Dropout'                  : _default_skip,
    'SpatialDropout2D'         : _default_skip,
    'SpatialDropout1D'         : _default_skip,
}


def _check_unsupported_layers(model):
    for layer in model.layers:
        if type(layer).__name__ not in _convert_map:
            raise ValueError("Keras layer {} not supported.".format(type(layer).__name__))


def keras_op_to_relay(inexpr, keras_layer, outname, etab):
    """Convert a Keras layer to a Relay expression and update the expression table.

    Parameters
    ----------
    inexpr : relay.expr.Expr or a list of it
        The input Relay expression(s).

    keras_layer : keras.layers
        The Keras layer to be converted.

    outname : str
        Name of the output Relay expression.

    etab : relay.frontend.common.ExprTable
        The global expression table to be updated.
    """
    if type(keras_layer).__name__ not in _convert_map:
        raise NotImplementedError("{} is not supported".format((type(keras_layer).__name__)))
    outs = _convert_map[type(keras_layer).__name__](inexpr, keras_layer, etab)
    outs = _as_list(outs)
    for t_idx, out in enumerate(outs):
        name = outname + ":" + str(t_idx)
        etab.set_expr(name, out)


def from_keras(model, shape_dict):
    """Convert keras model to relay Function.

    Parameters
    ----------
    model : keras.engine.training.Model
        The keras model to be converted.

    shape_dict : dict of str to int list/tuple
        Input shapes of the model.

    Returns
    -------
    func : tvm.relay.Function
        Compatible relay Function.

    params : dict of str to tvm.NDArray
        The parameter dict to be used by relay.
    """
    try:
        import keras
    except ImportError:
        raise ImportError('Keras must be installed')
    assert isinstance(model, keras.engine.training.Model)
    if keras.backend.backend() != 'tensorflow':
        raise ValueError("Keras frontend currently supports tensorflow backend only.")
    if keras.backend.image_data_format() != 'channels_last':
        raise ValueError("Keras frontend currently supports data_format = channels_last only.")
    _check_unsupported_layers(model)

    etab = ExprTable()
    for keras_layer in model.layers:
        if isinstance(keras_layer, keras.engine.InputLayer):
            input_name = keras_layer.name
            shape = shape_dict[input_name] if input_name in shape_dict else None
            etab.set_expr(input_name, _expr.var(input_name, shape=shape))
        else:
            inbound_nodes = keras_layer.inbound_nodes if hasattr(keras_layer, 'inbound_nodes') \
                       else keras_layer._inbound_nodes if hasattr(keras_layer, '_inbound_nodes') \
                       else None
            if inbound_nodes is None:
                raise TypeError("Unknown layer type or unsupported Keras version : {}"
                                .format(keras_layer))
            for node_idx, node in enumerate(inbound_nodes):
                # If some nodes in imported model is not relevant to the current model,
                # skip such layers. model._network_nodes contains keys of all nodes relevant
                # to the current model.
                if not model._node_key(keras_layer, node_idx) in model._network_nodes:
                    continue
                inexpr = []
                # Since Keras allows creating multiple layers from the same name instance,
                # we append node index to the expr name to make it unique.
                # The one exception is InputLayer. Changing input variable names after conversion
                # would confuse users, so we should keep them as far as possible. Fortunately,
                # they are named uniquely to input_1, input_2, input_3... by default.
                zip_node = zip(node.node_indices, node.tensor_indices, node.inbound_layers)
                for n_idx, t_idx, inbound_layer in zip_node:
                    if isinstance(inbound_layer, keras.engine.InputLayer):
                        expr_name = inbound_layer.name
                    else:
                        expr_name = inbound_layer.name + ':' + str(n_idx) + ':' + str(t_idx)
                    expr = etab.get_expr(expr_name)
                    inexpr.append(expr)
                if len(inexpr) == 1:
                    inexpr = inexpr[0]
                keras_op_to_relay(inexpr, keras_layer, keras_layer.name + ':' + str(node_idx), etab)
    # model._output_coordinates contains out_node(oc[0]), node_index(oc[1]) and tensor_index(oc[2])
    # Get all output nodes in etab using the name made from above values.
    # The out exprs were added to etab in keras_op_to_relay using this name.
    outexpr = [etab.get_expr(oc[0].name + ":" + str(oc[1]) + ":" + str(oc[2])) \
               for oc in model._output_coordinates]
    outexpr = outexpr[0] if len(outexpr) == 1 else _expr.Tuple(outexpr)
    func = _expr.Function(ir_pass.free_vars(outexpr), outexpr)
    params = {k:_nd.array(np.array(v, dtype=np.float32)) for k, v in etab.params.items()}
    return func, params
