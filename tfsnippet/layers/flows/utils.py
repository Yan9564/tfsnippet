import tensorflow as tf

from tfsnippet.ops import assert_rank_at_least
from tfsnippet.utils import (add_name_arg_doc, get_static_shape, get_shape,
                             assert_deps, broadcast_to_shape)

__all__ = [
    'is_log_det_shape_matches_input',
    'assert_log_det_shape_matches_input',
    'broadcast_log_det_against_input',
]


@add_name_arg_doc
def is_log_det_shape_matches_input(log_det, input, value_ndims, name=None):
    """
    Check whether or not the shape of `log_det` matches the shape of `input`.

    Basically, the shapes of `log_det` and `input` should satisfy::

        if value_ndims > 0:
            assert(log_det.shape == input.shape[:-value_ndims])
        else:
            assert(log_det.shape == input.shape)

    Args:
        log_det: Tensor, the log-determinant.
        input: Tensor, the input.
        value_ndims (int): The number of dimensions of each values sample.

    Returns:
        bool or tf.Tensor: A boolean or a tensor, indicating whether or not
            the shape of `log_det` matches the shape of `input`.
    """
    log_det = tf.convert_to_tensor(log_det)
    input = tf.convert_to_tensor(input)
    value_ndims = int(value_ndims)

    with tf.name_scope(name or 'is_log_det_shape_matches_input',
                       values=[log_det, input]):
        log_det_shape = get_static_shape(log_det)
        input_shape = get_static_shape(input)

        # if both shapes have deterministic ndims, we can compare each axis
        # separately.
        if log_det_shape is not None and input_shape is not None:
            if len(log_det_shape) + value_ndims != len(input_shape):
                return False
            dynamic_axis = []

            for i, (a, b) in enumerate(zip(log_det_shape, input_shape)):
                if a is None or b is None:
                    dynamic_axis.append(i)
                elif a != b:
                    return False

            if not dynamic_axis:
                return True

            log_det_shape = get_shape(log_det)
            input_shape = get_shape(input)
            return tf.reduce_all([
                tf.equal(log_det_shape[i], input_shape[i])
                for i in dynamic_axis
            ])

        # otherwise we need to do a fully dynamic check, including check
        # ``log_det.ndims + value_ndims == input_shape.ndims``
        is_ndims_matches = tf.equal(
            tf.rank(log_det) + value_ndims, tf.rank(input))
        log_det_shape = get_shape(log_det)
        input_shape = get_shape(input)
        if value_ndims > 0:
            input_shape = input_shape[:-value_ndims]

        return tf.cond(
            is_ndims_matches,
            lambda: tf.reduce_all(tf.equal(
                # The following trick ensures we're comparing two tensors
                # with the same shape, such as to avoid some potential issues
                # about the cond operation.
                tf.concat([log_det_shape, input_shape], 0),
                tf.concat([input_shape, log_det_shape], 0),
            )),
            lambda: tf.constant(False, dtype=tf.bool)
        )


@add_name_arg_doc
def assert_log_det_shape_matches_input(log_det, input, value_ndims, name=None):
    """
    Assert the shape of `log_det` matches the shape of `input`.

    Args:
        log_det: Tensor, the log-determinant.
        input: Tensor, the input.
        value_ndims (int): The number of dimensions of each values sample.

    Returns:
        tf.Operation or None: The assertion operation, or None if the
            assertion can be made statically.
    """
    log_det = tf.convert_to_tensor(log_det)
    input = tf.convert_to_tensor(input)
    value_ndims = int(value_ndims)

    with tf.name_scope(name or 'assert_log_det_shape_matches_input',
                       values=[log_det, input]):
        cmp_result = is_log_det_shape_matches_input(log_det, input, value_ndims)
        error_message = (
            'The shape of `log_det` does not match the shape of '
            '`input`: log_det {!r} vs input {!r}, value_ndims is {!r}'.
            format(log_det, input, value_ndims)
        )

        if cmp_result is False:
            raise AssertionError(error_message)

        elif cmp_result is True:
            return None

        else:
            return tf.assert_equal(cmp_result, True, message=error_message)


@add_name_arg_doc
def broadcast_log_det_against_input(log_det, input, value_ndims, name=None):
    """
    Broadcast the shape of `log_det` to match the shape of `input`.

    Args:
        log_det: Tensor, the log-determinant.
        input: Tensor, the input.
        value_ndims (int): The number of dimensions of each values sample.

    Returns:
        tf.Tensor: The broadcasted log-determinant.
    """
    log_det = tf.convert_to_tensor(log_det)
    input = tf.convert_to_tensor(input)
    value_ndims = int(value_ndims)

    with tf.name_scope(name or 'broadcast_log_det_to_input_shape',
                       values=[log_det, input]):
        shape = get_shape(input)
        if value_ndims > 0:
            err_msg = (
                'Cannot broadcast `log_det` against `input`: log_det is {}, '
                'input is {}, value_ndims is {}.'.
                format(log_det, input, value_ndims)
            )
            with assert_deps([assert_rank_at_least(
                    input, value_ndims, message=err_msg)]):
                shape = shape[:-value_ndims]

        return broadcast_to_shape(log_det, shape)
