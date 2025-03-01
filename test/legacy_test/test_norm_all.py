# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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

import unittest

import numpy as np
from op_test import OpTest, convert_float_to_uint16

import paddle
from paddle import _C_ops, base
from paddle.base import core
from paddle.base.framework import in_dygraph_mode


# hack method for test p_norm final state
def p_norm_python_api(
    x, p=2.0, axis=-1, epsilon=1e-12, keepdim=False, as_vector=False
):
    if in_dygraph_mode():
        return _C_ops.p_norm(x, p, axis, epsilon, keepdim, as_vector)


def p_norm(x, axis, porder, keepdims=False, reduce_all=False):
    r = []
    if axis is None or reduce_all:
        x = x.flatten()
        if porder == np.inf:
            r = np.amax(np.abs(x), keepdims=keepdims)
        elif porder == -np.inf:
            r = np.amin(np.abs(x), keepdims=keepdims)
        else:
            r = np.linalg.norm(x, ord=porder, keepdims=keepdims)
    elif isinstance(axis, list or tuple) and len(axis) == 2:
        if porder == np.inf:
            axis = tuple(axis)
            r = np.amax(np.abs(x), axis=axis, keepdims=keepdims)
        elif porder == -np.inf:
            axis = tuple(axis)
            r = np.amin(np.abs(x), axis=axis, keepdims=keepdims)
        elif porder == 0:
            axis = tuple(axis)
            r = x.astype(bool)
            r = np.sum(r, axis, keepdims=keepdims)
        elif porder == 1:
            axis = tuple(axis)
            r = np.sum(np.abs(x), axis, keepdims=keepdims)
        else:
            axis = tuple(axis)
            xp = np.power(np.abs(x), porder)
            s = np.sum(xp, axis=axis, keepdims=keepdims)
            r = np.power(s, 1.0 / porder)
    else:
        if isinstance(axis, list):
            axis = tuple(axis)
        r = np.linalg.norm(x, ord=porder, axis=axis, keepdims=keepdims)
    r = r.astype(x.dtype)

    return r


def numpy_frobenius_norm(x, axis=None, keepdims=False):
    if isinstance(axis, list):
        axis = tuple(axis)
    if axis is None:
        x = x.reshape(1, x.size)
    r = np.linalg.norm(x, ord='fro', axis=axis, keepdims=keepdims).astype(
        x.dtype
    )
    return r


def numpy_nuclear_norm(x, axis=None, keepdims=False):
    if isinstance(axis, list):
        axis = tuple(axis)
    r = np.linalg.norm(x, ord='nuc', axis=axis, keepdims=keepdims).astype(
        x.dtype
    )
    return r


def frobenius_norm(x, dim, keep_dim, reduce_all):
    return paddle.linalg.norm(x, p='fro', axis=dim, keepdim=keep_dim)


def nuclear_norm(x, dim, keep_dim, reduce_all):
    return paddle.linalg.norm(x, p='nuc', axis=dim, keepdim=keep_dim)


class TestFrobeniusNormOp(OpTest):
    def setUp(self):
        self.python_api = frobenius_norm
        self.op_type = "frobenius_norm"
        self.init_test_case()
        self.init_dtype()
        x = (np.random.random(self.shape) + 1.0).astype(self.dtype)
        norm = numpy_frobenius_norm(x, self.axis, self.keepdim)
        self.reduce_all = len(self.axis) == len(self.shape)
        self.inputs = {'X': x}
        self.attrs = {
            'dim': list(self.axis),
            'keep_dim': self.keepdim,
            'reduce_all': self.reduce_all,
        }
        self.outputs = {'Out': norm}

    def test_check_output(self):
        self.check_output(check_pir=True)

    def test_check_grad(self):
        self.check_grad(['X'], 'Out', check_pir=True)

    def init_test_case(self):
        self.shape = [2, 3, 4, 5]
        self.axis = (1, 2)
        self.keepdim = False

    def init_dtype(self):
        self.dtype = "float64"


class TestFrobeniusNormOp2(TestFrobeniusNormOp):
    def init_test_case(self):
        self.shape = [5, 5, 5]
        self.axis = (0, 1)
        self.keepdim = True

    def init_dtype(self):
        self.dtype = "float32"

    def test_check_grad(self):
        self.check_grad(['X'], 'Out', check_pir=True)


class TestPnormOp(OpTest):
    def setUp(self):
        self.op_type = "p_norm"
        self.python_api = p_norm_python_api
        self.init_test_case()
        self.init_dtype()
        x = (np.random.random(self.shape) + 0.5).astype(self.dtype)
        norm = p_norm(x, self.axis, self.porder, self.keepdim, self.asvector)
        self.inputs = {'X': x}
        self.attrs = {
            'epsilon': self.epsilon,
            'axis': self.axis,
            'keepdim': self.keepdim,
            'porder': float(self.porder),
            'asvector': self.asvector,
        }
        self.outputs = {'Out': norm}
        self.gradient = self.calc_gradient()

    def test_check_output(self):
        self.check_output()

    def test_check_grad(self):
        self.check_grad(['X'], 'Out')

    def init_test_case(self):
        self.shape = [2, 3, 4, 5]
        self.axis = 1
        self.epsilon = 1e-12
        self.porder = 2.0
        self.keepdim = False
        self.asvector = False

    def init_dtype(self):
        self.dtype = "float64"

    def calc_gradient(self):
        self.attrs = {
            'epsilon': self.epsilon,
            'axis': self.axis,
            'keepdim': self.keepdim,
            'porder': float(self.porder),
            'asvector': self.asvector,
        }
        x = self.inputs["X"]
        porder = self.attrs["porder"]
        axis = self.attrs["axis"]
        asvector = self.attrs["asvector"]
        x_dtype = x.dtype
        x = x.astype(np.float32) if x.dtype == np.float16 else x
        if porder == 0:
            grad = np.zeros(x.shape).astype(x.dtype)
        elif porder in [float("inf"), float("-inf")]:
            norm = p_norm(
                x, axis=axis, porder=porder, keepdims=True, reduce_all=asvector
            )
            x_abs = np.abs(x)
            grad = np.sign(x)
            grad[x_abs != norm] = 0.0
        else:
            norm = p_norm(
                x, axis=axis, porder=porder, keepdims=True, reduce_all=asvector
            )
            grad = (
                np.power(norm, 1 - porder)
                * np.power(np.abs(x), porder - 1)
                * np.sign(x)
            )

        numel = 1
        for s in x.shape:
            numel *= s
        divisor = numel if asvector else x.shape[axis]
        numel /= divisor
        return [grad.astype(x_dtype) * 1 / numel]


class TestPnormOp2(TestPnormOp):
    def init_test_case(self):
        self.shape = [3, 20, 3]
        self.axis = 2
        self.epsilon = 1e-12
        self.porder = 2.0
        self.keepdim = True
        self.asvector = False

    def init_dtype(self):
        self.dtype = "float32"

    def test_check_grad(self):
        self.check_grad(['X'], 'Out')


class TestPnormOp3(TestPnormOp):
    def init_test_case(self):
        self.shape = [3, 20, 3]
        self.axis = 2
        self.epsilon = 1e-12
        self.porder = np.inf
        self.keepdim = True
        self.asvector = False

    def init_dtype(self):
        self.dtype = "float32"

    def test_check_grad(self):
        self.check_grad(['X'], 'Out', user_defined_grads=self.gradient)


class TestPnormOp4(TestPnormOp):
    def init_test_case(self):
        self.shape = [3, 20, 3]
        self.axis = 2
        self.epsilon = 1e-12
        self.porder = -np.inf
        self.keepdim = True
        self.asvector = False

    def init_dtype(self):
        self.dtype = "float32"

    def test_check_grad(self):
        self.check_grad(['X'], 'Out', user_defined_grads=self.gradient)


class TestPnormOp5(TestPnormOp):
    def init_test_case(self):
        self.shape = [3, 20, 3]
        self.axis = 2
        self.epsilon = 1e-12
        self.porder = 0
        self.keepdim = True
        self.asvector = False

    def init_dtype(self):
        self.dtype = "float32"

    def test_check_grad(self):
        self.check_grad(['X'], 'Out', user_defined_grads=self.gradient)


class TestPnormOp6(TestPnormOp):
    def init_test_case(self):
        self.shape = [3, 20, 3]
        self.axis = -1
        self.epsilon = 1e-12
        self.porder = 2
        self.keepdim = False
        self.asvector = True

    def init_dtype(self):
        self.dtype = "float32"

    def test_check_grad(self):
        self.check_grad(['X'], 'Out', user_defined_grads=self.gradient)


def create_test_fp16_class(parent, max_relative_error=2e-3):
    @unittest.skipIf(
        not core.is_compiled_with_cuda(), "core is not compiled with CUDA"
    )
    class TestPnormFP16Op(parent):
        def init_dtype(self):
            self.dtype = "float16"

        def test_check_output(self):
            place = core.CUDAPlace(0)
            if core.is_float16_supported(place):
                self.check_output_with_place(place)

        def test_check_grad(self):
            place = core.CUDAPlace(0)
            if core.is_float16_supported(place):
                self.check_grad_with_place(
                    place,
                    ['X'],
                    'Out',
                    user_defined_grads=self.gradient,
                    max_relative_error=max_relative_error,
                )

    cls_name = "{}_{}".format(parent.__name__, "Fp16")
    TestPnormFP16Op.__name__ = cls_name
    globals()[cls_name] = TestPnormFP16Op


create_test_fp16_class(TestPnormOp)
create_test_fp16_class(TestPnormOp2)
create_test_fp16_class(TestPnormOp3)
create_test_fp16_class(TestPnormOp4)
create_test_fp16_class(TestPnormOp5)
create_test_fp16_class(TestPnormOp6)


@unittest.skipIf(
    not core.is_compiled_with_cuda(), "core is not compiled with CUDA"
)
class TestPnormBF16Op(OpTest):
    def setUp(self):
        self.op_type = "p_norm"
        self.python_api = p_norm_python_api
        self.init_test_case()
        self.x = (np.random.random(self.shape) + 0.5).astype(np.float32)
        self.norm = p_norm(
            self.x, self.axis, self.porder, self.keepdim, self.asvector
        )
        self.gradient = self.calc_gradient()
        self.inputs = {'X': convert_float_to_uint16(self.x)}
        self.attrs = {
            'epsilon': self.epsilon,
            'axis': self.axis,
            'keepdim': self.keepdim,
            'porder': float(self.porder),
            'asvector': self.asvector,
        }
        self.outputs = {'Out': convert_float_to_uint16(self.norm)}

    def test_check_output(self):
        place = core.CUDAPlace(0)
        self.check_output_with_place(place, atol=1e-3)

    def test_check_grad(self):
        place = core.CUDAPlace(0)
        self.check_grad_with_place(
            place,
            ['X'],
            'Out',
            user_defined_grads=self.gradient,
        )

    def init_test_case(self):
        self.shape = [2, 3, 4, 5]
        self.axis = 1
        self.epsilon = 1e-12
        self.porder = 2.0
        self.keepdim = False
        self.asvector = False

    def init_dtype(self):
        self.dtype = np.uint16

    def calc_gradient(self):
        self.attrs = {
            'epsilon': self.epsilon,
            'axis': self.axis,
            'keepdim': self.keepdim,
            'porder': float(self.porder),
            'asvector': self.asvector,
        }
        x = self.x
        porder = self.attrs["porder"]
        axis = self.attrs["axis"]
        asvector = self.attrs["asvector"]
        x_dtype = x.dtype
        x = x.astype(np.float32) if x.dtype == np.float16 else x
        if porder == 0:
            grad = np.zeros(x.shape).astype(x.dtype)
        elif porder in [float("inf"), float("-inf")]:
            norm = p_norm(
                x, axis=axis, porder=porder, keepdims=True, reduce_all=asvector
            )
            x_abs = np.abs(x)
            grad = np.sign(x)
            grad[x_abs != norm] = 0.0
        else:
            norm = p_norm(
                x, axis=axis, porder=porder, keepdims=True, reduce_all=asvector
            )
            grad = (
                np.power(norm, 1 - porder)
                * np.power(np.abs(x), porder - 1)
                * np.sign(x)
            )

        numel = 1
        for s in x.shape:
            numel *= s
        divisor = numel if asvector else x.shape[axis]
        numel /= divisor
        return [grad.astype(x_dtype) * 1 / numel]


def run_fro(self, p, axis, shape_x, dtype, keep_dim, check_dim=False):
    with base.program_guard(base.Program()):
        data = paddle.static.data(name="X", shape=shape_x, dtype=dtype)
        out = paddle.norm(x=data, p=p, axis=axis, keepdim=keep_dim)
        place = base.CPUPlace()
        exe = base.Executor(place)
        np_input = (np.random.rand(*shape_x) + 1.0).astype(dtype)
        expected_result = numpy_frobenius_norm(
            np_input, axis=axis, keepdims=keep_dim
        )
        (result,) = exe.run(feed={"X": np_input}, fetch_list=[out])
    self.assertEqual((np.abs(result - expected_result) < 1e-6).all(), True)
    if keep_dim and check_dim:
        self.assertEqual(
            (
                np.abs(np.array(result.shape) - np.array(expected_result.shape))
                < 1e-6
            ).all(),
            True,
        )


def check_nuc_static(self, p, axis, shape_x, dtype, keep_dim, check_dim=False):
    with base.program_guard(base.Program()):
        data = paddle.static.data(name="X", shape=shape_x, dtype=dtype)
        out = paddle.norm(x=data, p=p, axis=axis, keepdim=keep_dim)
        place = base.CPUPlace()
        exe = base.Executor(place)
        np_input = (np.random.rand(*shape_x) + 1.0).astype(dtype)
        expected_result = numpy_nuclear_norm(
            np_input, axis=axis, keepdims=keep_dim
        )
        (result,) = exe.run(feed={"X": np_input}, fetch_list=[out])
    np.testing.assert_allclose(result, expected_result, rtol=1e-6, atol=1e-8)
    if keep_dim and check_dim:
        np.testing.assert_equal(result.shape, expected_result.shape)


def check_nuc_dygraph(self, p, axis, shape_x, dtype, keep_dim, check_dim=False):
    x_numpy = (np.random.random(shape_x) + 1.0).astype(dtype)
    expected_result = numpy_nuclear_norm(x_numpy, axis, keep_dim)
    x_paddle = paddle.to_tensor(x_numpy)
    result = paddle.norm(x=x_paddle, p=p, axis=axis, keepdim=keep_dim)
    result = result.numpy()
    np.testing.assert_allclose(result, expected_result, rtol=1e-6, atol=1e-8)
    if keep_dim and check_dim:
        np.testing.assert_equal(result.shape, expected_result.shape)


def run_pnorm(self, p, axis, shape_x, dtype, keep_dim, check_dim=False):
    with base.program_guard(base.Program()):
        data = paddle.static.data(name="X", shape=shape_x, dtype=dtype)
        out = paddle.norm(x=data, p=p, axis=axis, keepdim=keep_dim)
        place = base.CPUPlace()
        exe = base.Executor(place)
        np_input = (np.random.rand(*shape_x) + 1.0).astype(dtype)
        expected_result = p_norm(
            np_input, porder=p, axis=axis, keepdims=keep_dim
        ).astype(dtype)
        (result,) = exe.run(feed={"X": np_input}, fetch_list=[out])
    self.assertEqual((np.abs(result - expected_result) < 1e-6).all(), True)
    if keep_dim and check_dim:
        self.assertEqual(
            (
                np.abs(np.array(result.shape) - np.array(expected_result.shape))
                < 1e-6
            ).all(),
            True,
        )


def run_graph(self, p, axis, shape_x, dtype):
    paddle.disable_static()
    shape = [2, 3, 4]
    np_input = np.arange(24).astype('float32') - 12
    np_input = np_input.reshape(shape)
    x = paddle.to_tensor(np_input)
    # [[[-12. -11. -10.  -9.] [ -8.  -7.  -6.  -5.] [ -4.  -3.  -2.  -1.]]
    # [[  0.   1.   2.   3.] [  4.   5.   6.   7.] [  8.   9.  10.  11.]]]
    out_pnorm = paddle.norm(x, p=2, axis=-1)

    # compute frobenius norm along last two dimensions.
    out_fro = paddle.norm(x, p='fro')
    out_fro = paddle.norm(x, p='fro', axis=0)
    out_fro = paddle.norm(x, p='fro', axis=[0, 1])
    # compute nuclear norm.
    out_nuc = paddle.norm(x, p='nuc', axis=[0, 1])
    # compute 2-order  norm along [0,1] dimension.
    out_pnorm = paddle.norm(x, p=2, axis=[0, 1])
    out_pnorm = paddle.norm(x, p=2)
    # out_pnorm = [17.43559577 16.91153453 16.73320053 16.91153453]
    # compute inf-order  norm
    out_pnorm = paddle.norm(x, p=np.inf)
    # out_pnorm = [12.]
    out_pnorm = paddle.norm(x, p=np.inf, axis=0)
    # out_pnorm = [[0. 1. 2. 3.] [4. 5. 6. 5.] [4. 3. 2. 1.]]

    # compute -inf-order  norm
    out_pnorm = paddle.norm(x, p=-np.inf)
    # out_pnorm = [0.]
    out_pnorm = paddle.norm(x, p=-np.inf, axis=0)
    # out_fro = [17.43559577 16.91153453 16.73320053 16.91153453]
    paddle.enable_static()


class API_NormTest(unittest.TestCase):
    def test_basic(self):
        keep_dims = {False, True}
        for keep in keep_dims:
            run_fro(
                self,
                p='fro',
                axis=None,
                shape_x=[2, 3, 4],
                dtype="float32",
                keep_dim=keep,
            )
            run_fro(
                self,
                p='fro',
                axis=[0, 1],
                shape_x=[2, 3, 4],
                dtype="float64",
                keep_dim=keep,
                check_dim=True,
            )
            check_nuc_static(
                self,
                p='nuc',
                axis=[0, 1],
                shape_x=[2, 3, 4],
                dtype='float64',
                keep_dim=keep,
                check_dim=True,
            )
            run_pnorm(
                self,
                p=2,
                axis=None,
                shape_x=[3, 4],
                dtype="float32",
                keep_dim=keep,
            )
            run_pnorm(
                self,
                p=2,
                axis=1,
                shape_x=[3, 4],
                dtype="float64",
                keep_dim=keep,
                check_dim=True,
            )
            run_pnorm(
                self,
                p=np.inf,
                axis=0,
                shape_x=[2, 3, 4],
                dtype="float32",
                keep_dim=keep,
                check_dim=True,
            )
            run_pnorm(
                self,
                p=np.inf,
                axis=None,
                shape_x=[2, 3, 4],
                dtype="float32",
                keep_dim=keep,
            )
            run_pnorm(
                self,
                p=-np.inf,
                axis=0,
                shape_x=[2, 3, 4],
                dtype="float64",
                keep_dim=keep,
                check_dim=True,
            )
            run_pnorm(
                self,
                p=-np.inf,
                axis=None,
                shape_x=[2, 3, 4],
                dtype="float64",
                keep_dim=keep,
            )
            run_pnorm(
                self,
                p=0,
                axis=1,
                shape_x=[3, 4],
                dtype="float64",
                keep_dim=keep,
                check_dim=True,
            )

            run_pnorm(
                self,
                p=1,
                axis=1,
                shape_x=[3, 4],
                dtype="float64",
                keep_dim=keep,
                check_dim=True,
            )
            run_pnorm(
                self,
                p=0,
                axis=None,
                shape_x=[3, 4],
                dtype="float64",
                keep_dim=keep,
                check_dim=True,
            )
            run_pnorm(
                self,
                p=2,
                axis=[0, 1],
                shape_x=[2, 3, 4],
                dtype="float64",
                keep_dim=keep,
                check_dim=True,
            )
            run_pnorm(
                self,
                p=2,
                axis=-1,
                shape_x=[2, 3, 4],
                dtype="float64",
                keep_dim=keep,
                check_dim=True,
            )
            run_pnorm(
                self,
                p=1,
                axis=[0, 1],
                shape_x=[2, 3, 4],
                dtype="float64",
                keep_dim=keep,
                check_dim=True,
            )
            run_pnorm(
                self,
                p=np.inf,
                axis=[0, 1],
                shape_x=[2, 3, 4],
                dtype="float64",
                keep_dim=keep,
                check_dim=True,
            )
            run_pnorm(
                self,
                p=-np.inf,
                axis=[0, 1],
                shape_x=[2, 3, 4],
                dtype="float64",
                keep_dim=keep,
                check_dim=True,
            )

    def test_dygraph(self):
        run_graph(self, p='fro', axis=None, shape_x=[2, 3, 4], dtype="float32")

        paddle.disable_static()
        keep_dims = {False, True}
        for keep in keep_dims:
            check_nuc_dygraph(
                self,
                p='nuc',
                axis=[0, 1],
                shape_x=[2, 3, 4],
                dtype='float64',
                keep_dim=keep,
                check_dim=True,
            )
            check_nuc_dygraph(
                self,
                p='nuc',
                axis=[1, 2],
                shape_x=[2, 3, 4, 5],
                dtype='float64',
                keep_dim=keep,
                check_dim=True,
            )
        paddle.enable_static()

    def test_name(self):
        with base.program_guard(base.Program()):
            x = paddle.static.data(name="x", shape=[10, 10], dtype="float32")
            y_1 = paddle.norm(x, p='fro', name='frobenius_name')
            y_2 = paddle.norm(x, p=2, name='pnorm_name')
            y_3 = paddle.norm(x, p='nuc', axis=[0, 1], name='nuclear_name')
            self.assertEqual(('frobenius_name' in y_1.name), True)
            self.assertEqual(('pnorm_name' in y_2.name), True)
            self.assertEqual(('nuclear_name' in y_3.name), True)

    def test_errors(self):
        with base.program_guard(base.Program(), base.Program()):

            def err_dtype(p, shape_x, xdtype, out=None):
                data = paddle.static.data(shape=shape_x, dtype=xdtype)
                paddle.norm(data, p=p, out=out)

            self.assertRaises(TypeError, err_dtype, "fro", [2, 2], "int64")
            self.assertRaises(ValueError, paddle.norm, "inf", [2], "int64")
            out = paddle.static.data(name="out", shape=[1], dtype="int64")
            self.assertRaises(
                TypeError, err_dtype, "fro", [2, 2], "float64", out
            )
            self.assertRaises(TypeError, err_dtype, 2, [10], "int64")
            self.assertRaises(TypeError, err_dtype, 2, [10], "float64", out)

            data = paddle.static.data(
                name="data_2d", shape=[2, 2], dtype="float64"
            )
            self.assertRaises(ValueError, paddle.norm, data, p="unsupport norm")
            self.assertRaises(ValueError, paddle.norm, data, p=[1])
            self.assertRaises(ValueError, paddle.norm, data, p=[1], axis=-1)
            self.assertRaises(ValueError, paddle.norm, 0, [1, 0], "float64")
            data = paddle.static.data(
                name="data_3d", shape=[2, 2, 2], dtype="float64"
            )
            self.assertRaises(
                ValueError, paddle.norm, data, p='unspport', axis=[-3, -2, -1]
            )

        with base.dygraph.guard():
            # The size of input in Norm should not be 0.
            def test_0_size():
                array = np.array([], dtype=np.float32)
                x = paddle.to_tensor(np.reshape(array, [0, 0]), dtype='float32')
                paddle.linalg.norm(x, axis=0)

            self.assertRaises(ValueError, test_0_size)


if __name__ == '__main__':
    paddle.enable_static()
    unittest.main()
