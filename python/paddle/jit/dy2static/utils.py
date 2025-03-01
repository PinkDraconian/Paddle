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

from __future__ import annotations

import atexit
import builtins
import copy
import functools
import importlib.util
import inspect
import os
import shutil
import sys
import tempfile
import textwrap
import types
import warnings
from importlib.machinery import SourceFileLoader

import numpy as np

import paddle
from paddle import base, get_flags, set_flags  # noqa: F401
from paddle.base import backward, core, framework, unique_name
from paddle.base.data_feeder import convert_dtype
from paddle.base.layer_helper import LayerHelper
from paddle.base.wrapped_decorator import signature_safe_contextmanager
from paddle.framework import CUDAPinnedPlace
from paddle.utils import flatten, gast

from .ast_utils import ast_to_source_code
from .utils_helper import (  # noqa: F401
    DYGRAPH_MODULE_PREFIX,
    DYGRAPH_TO_STATIC_MODULE_PREFIX,
    PADDLE_MODULE_PREFIX,
    _is_api_in_module_helper,
    index_in_list,
    is_api_in_module,
    is_dygraph_api,
    is_paddle_api,
)

__all__ = []

# Note(Aurelius): Do not forget the dot `.` to distinguish other
# module such as paddlenlp.
GET_ARGS_FUNC_PREFIX = 'get_args'
SET_ARGS_FUNC_PREFIX = 'set_args'
ALREADY_D2S = '__already_d2s'
ARGS_NAME = '__args'
# NOTE(liym27): Please use `getattr(ast_node, ORIGI_INFO)` instead of . operation to get the original information of ast node.
ORIGI_INFO = "Original information of source code for ast node."

DEL_TEMP_DIR = True  # A flag to avoid atexit.register more than once
FOR_ITER_INDEX_PREFIX = '__for_loop_var_index'
FOR_ITER_TUPLE_PREFIX = '__for_loop_iter_tuple'
FOR_ITER_TARGET_PREFIX = '__for_loop_iter_target'
FOR_ITER_ITERATOR_PREFIX = '__for_loop_iter_iterator'
FOR_ITER_TUPLE_INDEX_PREFIX = '__for_loop_iter_tuple_index'
FOR_ITER_VAR_LEN_PREFIX = '__for_loop_var_len'
FOR_ITER_VAR_NAME_PREFIX = '__for_loop_iter_var'
FOR_ITER_ZIP_TO_LIST_PREFIX = '__for_loop_iter_zip'

RE_PYNAME = '[a-zA-Z0-9_]+'
RE_PYMODULE = r'[a-zA-Z0-9_]+\.'

# Assign not support float64, use float32 value as magic number.
RETURN_NO_VALUE_VAR_NAME = "__no_value_return_var"
RETURN_NO_VALUE_MAGIC_NUM = 1.77113e27

TRUE_FUNC_PREFIX = 'true_fn'
FALSE_FUNC_PREFIX = 'false_fn'

WHILE_CONDITION_PREFIX = 'while_condition'
WHILE_BODY_PREFIX = 'while_body'
FOR_CONDITION_PREFIX = 'for_loop_condition'
FOR_BODY_PREFIX = 'for_loop_body'

GRAD_PREFIX = 'grad/'
GRAD_SUFFIX = '@GRAD'

NO_SHAPE_VAR_TYPE = [
    core.VarDesc.VarType.READER,
    core.VarDesc.VarType.STEP_SCOPES,
    core.VarDesc.VarType.FEED_MINIBATCH,
    core.VarDesc.VarType.FETCH_LIST,
]


class BaseNodeVisitor(gast.NodeVisitor):
    """
    Implement customized NodeVisitor inherited from gast.NodeVisitor.
    Ancestor nodes are traced to easily support more operations of currently
    visited node.
    """

    def __init__(self):
        self.ancestor_nodes = []

    def visit(self, node):
        """Visit a node."""
        self.ancestor_nodes.append(node)

        method = 'visit_' + node.__class__.__name__
        visitor = getattr(self, method, self.generic_visit)
        ret = visitor(node)
        self.ancestor_nodes.pop()
        return ret


def get_parent_mapping(root):
    to_parent: dict[gast.AST, gast.AST] = {}
    for node in gast.walk(root):
        for child in gast.iter_child_nodes(node):
            to_parent[child] = node
    return to_parent


dygraph_class_to_static_api = {
    "CosineDecay": "cosine_decay",
    "ExponentialDecay": "exponential_decay",
    "InverseTimeDecay": "inverse_time_decay",
    "NaturalExpDecay": "natural_exp_decay",
    "NoamDecay": "noam_decay",
    "PiecewiseDecay": "piecewise_decay",
    "PolynomialDecay": "polynomial_decay",
}


def data_layer_not_check(name, shape, dtype='float32', lod_level=0):
    """
    This function creates a Tensor on the global block. The created Tensor
    doesn't check the dtype and the shape of feed data because dygraph input
    data can be various-length. This API is used in translating dygraph into
    static graph.

    Note:
        The default :code:`stop_gradient` attribute of the Tensor created by
        this API is true, which means the gradient won't be passed backward
        through the data Tensor. Set :code:`var.stop_gradient = False` If
        user would like to pass backward gradient.

    Args:
       name (str): The name/alias of the Tensor, see :ref:`api_guide_Name`
           for more details.
       shape (list|tuple): List|Tuple of integers declaring the shape. You can
           set "None" at a dimension to indicate the dimension can be of any
           size. For example, it is useful to set changeable batch size as "None"
       dtype (np.dtype|VarType|str, optional): The type of the data. Supported
           dtype: bool, float16, float32, float64, int8, int16, int32, int64,
           uint8. Default: float32
       lod_level (int, optional): The LoD level of the LoDTensor. Usually users
           don't have to set this value. Default: 0

    Returns:
        Tensor: The global Tensor that gives access to the data.
    """
    helper = LayerHelper('data', **locals())
    shape = list(shape)
    for i in range(len(shape)):
        if shape[i] is None:
            shape[i] = -1

    return helper.create_global_variable(
        name=name,
        shape=shape,
        dtype=dtype,
        type=core.VarDesc.VarType.LOD_TENSOR,
        stop_gradient=True,
        lod_level=lod_level,
        is_data=True,
        need_check_feed=False,
    )


def create_undefined_variable():
    var = data_layer_not_check(
        unique_name.generate("undefined_var"), [1], "float64"
    )
    var.stop_gradient = False
    # the variable is created in block(0), we append assign in block(0) either.
    helper = LayerHelper('create_undefined_variable', **locals())
    saved_block_ids = helper.main_program.current_block_idx
    helper.main_program.current_block_idx = 0
    paddle.assign(RETURN_NO_VALUE_MAGIC_NUM, var)
    helper.main_program.current_block_idx = saved_block_ids
    return var


class UndefinedVar:
    def __init__(self, name):
        self.name = name

    def check(self):
        raise UnboundLocalError(
            "local variable '{}' should be created before using it."
        )


class Dygraph2StaticException(Exception):
    def __init__(self, message):
        super().__init__(message)


def saw(x):
    if isinstance(x, UndefinedVar):
        return x.check()
    else:
        return x


def parse_arg_and_kwargs(function):
    """
    Returns full argument names as list. e.g ['x', 'y', 'z']
    """
    fullargspec = inspect.getfullargspec(function)
    arg_names = fullargspec.args
    if arg_names and 'self' == arg_names[0]:
        arg_names = fullargspec.args[1:]

    # parse default kwargs
    default_kwargs = {}
    default_values = fullargspec.defaults
    if default_values:
        assert len(default_values) <= len(arg_names)
        default_kwarg_names = arg_names[-len(default_values) :]
        default_kwargs = dict(zip(default_kwarg_names, default_values))

    return arg_names, default_kwargs


def parse_varargs_name(function):
    """
    Returns varargs name string of function. e.g: 'input' from `foo(x, *input)`
    """
    fullargspec = inspect.getfullargspec(function)
    varargs = fullargspec.varargs
    return varargs


def type_name(v):
    return type(v).__name__


def make_hashable(x, error_msg=None):
    """
    Makes input `x` hashable.

    For some unhashable objects, such as `dict/list/set/np.ndarray`,applying hash function by using their values.
    """
    if isinstance(x, (tuple, list, set)):
        return tuple(map(make_hashable, x))

    try:
        hash(x)
    except TypeError:
        if isinstance(x, np.ndarray):
            # Note: `tostring()` will return the binary data from np.ndarray that
            # means different value will lead to different hash code.
            return hash(x.tostring())
        elif isinstance(x, dict):
            return tuple(map(make_hashable, x.values()))

        error_msg = error_msg or "Requires a hashable object."
        raise ValueError(error_msg + " But received type: %s" % type_name(x))

    return x


# NOTE(Aurelius84): Consider the following paddle inner API as common case to
# apply @to_static code transformation as usual. Because they contains
# user-defined layer, like paddle.distributed.auto_parallel.helper.ProxyLayer.
AS_NOT_INNER_FUNC_LIST = {"paddle.nn.layer.container.Sequential"}


def as_not_paddle_func(path):
    """
    Append API or class as ignored case for is_paddle_func, and they
    will be retured False while calling is_paddle_func(func).
    """
    global INNER_FUNC_WHITE_LIST
    AS_NOT_INNER_FUNC_LIST.add(path)


def is_paddle_func(func, ignore_white_list=True):
    """
    Return True if function is defined in Paddle module.
    Skip to check APIs in white list if specifying ignore_white_list as True.
    """

    def in_white_list(module, func_name):
        if func_name is None:
            return False
        return (module.__name__ + '.' + func_name) in AS_NOT_INNER_FUNC_LIST

    try:
        if isinstance(func, functools.partial):
            func = func.func

        func_name = getattr(func, '__name__', None)
        if inspect.ismethod(func):
            func_name = func.__self__.__class__.__name__
            func = func.__func__
        elif hasattr(func, '__class__'):  # for nn.Sequential
            func_name = func.__class__.__name__

        m = inspect.getmodule(func)
        flag = m is not None and m.__name__.startswith(PADDLE_MODULE_PREFIX)
        if ignore_white_list:
            flag = flag and not in_white_list(m, func_name)

        return flag
    except Exception:
        return False


def _delete_keywords_from(node):
    assert isinstance(node, gast.Call)
    func_src = ast_to_source_code(node.func)

    full_args = eval(f"inspect.getfullargspec({func_src})")
    full_args_name = full_args[0]

    node.keywords = [k for k in node.keywords if k.arg in full_args_name]


def to_static_api(dygraph_class):
    if dygraph_class in dygraph_class_to_static_api:
        return dygraph_class_to_static_api[dygraph_class]
    else:
        raise NotImplementedError(
            f"Paddle dygraph API {dygraph_class} cannot be converted "
            "to static graph at present."
        )


def _add_keywords_to(node, dygraph_api_name):
    assert isinstance(node, gast.Call)
    if dygraph_api_name == "Linear":
        for ast_keyword in node.keywords:
            if ast_keyword.arg == "output_dim":
                ast_keyword.arg = "size"

        node.keywords.append(
            gast.keyword(
                arg="num_flatten_dims", value=gast.Constant(value=-1, kind=None)
            )
        )

    if dygraph_api_name == "BilinearTensorProduct":
        for ast_keyword in node.keywords:
            if ast_keyword.arg == "output_dim":
                ast_keyword.arg = "size"

    if dygraph_api_name == "PRelu":
        for ast_keyword in node.keywords:
            if ast_keyword.arg == "input":
                ast_keyword.arg = "x"


def to_static_ast(node, class_node):
    assert isinstance(node, gast.Call)
    assert isinstance(class_node, gast.Call)
    static_api = to_static_api(class_node.func.attr)

    node.func = gast.Attribute(
        attr=static_api,
        ctx=gast.Load(),
        value=gast.Attribute(
            attr='layers',
            ctx=gast.Load(),
            value=gast.Name(
                ctx=gast.Load(), id='base', annotation=None, type_comment=None
            ),
        ),
    )

    update_args_of_func(node, class_node, 'forward')

    node.args.extend(class_node.args)
    node.keywords.extend(class_node.keywords)
    _add_keywords_to(node, class_node.func.attr)
    _delete_keywords_from(node)

    gast.fix_missing_locations(node)

    return node


def update_args_of_func(node, dygraph_node, method_name):
    assert isinstance(node, gast.Call)
    if method_name not in ["__init__", "forward"]:
        raise ValueError(
            "The method name of class to update args should be '__init__' or 'forward'"
        )

    class_src = ast_to_source_code(dygraph_node.func)

    if method_name == "__init__" or eval(
        f"issubclass({class_src}, paddle.nn.Layer)"
    ):
        full_args = eval(f"inspect.getfullargspec({class_src}.{method_name})")
        full_args_name = [
            arg_name for arg_name in full_args[0] if arg_name != "self"
        ]
    else:
        full_args_name = []
    added_keywords = []
    for idx, arg in enumerate(node.args):
        added_keywords.append(gast.keyword(arg=full_args_name[idx], value=arg))

    node.args = []
    node.keywords = added_keywords + node.keywords


def create_api_shape_node(tensor_shape_node):
    assert isinstance(
        tensor_shape_node, (gast.Name, gast.Attribute, gast.Subscript)
    )

    if isinstance(tensor_shape_node, gast.Name):
        api_shape_node = gast.Call(
            func=gast.parse('paddle.shape').body[0].value,
            args=[tensor_shape_node],
            keywords=[],
        )
        return api_shape_node

    if isinstance(tensor_shape_node, gast.Attribute):
        api_shape_node = gast.Call(
            func=gast.parse('paddle.shape').body[0].value,
            args=[tensor_shape_node.value],
            keywords=[],
        )
        return api_shape_node

    if isinstance(tensor_shape_node, gast.Subscript):
        result_node = copy.deepcopy(tensor_shape_node)
        result_node.value = create_api_shape_node(result_node.value)
        return result_node


def get_constant_variable_node(name, value, shape=[1], dtype='int64'):
    return gast.parse(
        f'{name} = paddle.full({str(shape)}, "{str(value)}", {dtype})'
    )


def get_attribute_full_name(node):
    assert isinstance(
        node, gast.Attribute
    ), "Input non-Attribute node to get attribute full name"
    return ast_to_source_code(node).strip()


def generate_name_node(name_ids, ctx=gast.Load(), gen_tuple_if_single=False):
    """
    If name_ids is list or tuple or set with multiple strings, this function
    generates gast.Tuple of gast.Name.
    If the name_ids is single string or contains only 1 string, this function
    returns gast.Name if gen_tuple_if_single==False else returns gast.Tuple
    with only one gast.Name

    This function is used at several gast.Return statements.
    """
    if isinstance(name_ids, str):
        name_ids = [name_ids]
    if not isinstance(name_ids, (list, tuple, set)):
        raise TypeError(
            'name_ids must be list or tuple or set, but received %s'
            % type(type(name_ids))
        )

    def create_node_for_name(name):
        if '.' not in name:
            return gast.Name(
                id=name, ctx=ctx, annotation=None, type_comment=None
            )
        return gast.parse(name).body[0].value

    gast_names = [create_node_for_name(name_id) for name_id in name_ids]
    if len(gast_names) == 1 and not gen_tuple_if_single:
        name_node = gast_names[0]
    else:
        name_node = gast.Tuple(elts=gast_names, ctx=ctx)
    return name_node


def create_funcDef_node(nodes, name, input_args, return_name_ids):
    """
    Wrapper all statements of nodes into one ast.FunctionDef, which can be
    called by ast.Call.
    """
    nodes = copy.copy(nodes)
    # add return statement
    if return_name_ids:
        nodes.append(gast.Return(value=generate_name_node(return_name_ids)))
    else:
        nodes.append(gast.Return(value=None))
    func_def_node = gast.FunctionDef(
        name=name,
        args=input_args,
        body=nodes,
        decorator_list=[],
        returns=None,
        type_comment=None,
    )
    return func_def_node


def create_assign_node(name, node):
    """
    Creates a `gast.Assign` node by given name_id as target and node as value.
    """
    targets = generate_name_node(name, ctx=gast.Store())
    assign_node = gast.Assign(targets=[targets], value=node)
    return targets, assign_node


def get_temp_dir():
    """
    Return @to_static temp directory.
    """
    dir_name = f"paddle/to_static_tmp/{os.getpid()}"
    temp_dir = os.path.join(os.path.expanduser('~/.cache'), dir_name)
    is_windows = sys.platform.startswith('win')
    if is_windows:
        temp_dir = os.path.normpath(temp_dir)

    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)

    return temp_dir


def ast_to_func(ast_root, dyfunc, delete_on_exit=True):
    """
    Transform modified AST of decorated function into python callable object.
    TODO: If only decorate one of inner function instead of decorating the main
    function, the other inner functions are invisible for the decorated function.
    """

    def remove_if_exit(dir_path):
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path)

    def func_prefix(func):
        pre_fix = func.__name__
        if hasattr(func, '__self__'):
            try:
                pre_fix = func.__self__.__class__.__name__ + '_' + func.__name__
            except:
                pass
        return pre_fix

    source = ast_to_source_code(ast_root)
    source = _inject_import_statements() + source
    temp_dir = get_temp_dir()
    f = tempfile.NamedTemporaryFile(
        mode='w',
        prefix=func_prefix(dyfunc),
        suffix='.py',
        delete=False,
        dir=temp_dir,
        encoding='utf-8',
    )
    with f:
        module_name = os.path.basename(f.name[:-3])
        f.write(source)

    global DEL_TEMP_DIR
    if delete_on_exit and DEL_TEMP_DIR:
        # Clear temporary files in TEMP_DIR while exitting Python process
        atexit.register(remove_if_exit, dir_path=temp_dir)
        DEL_TEMP_DIR = False

    func_name = dyfunc.__name__
    loader = SourceFileLoader(module_name, f.name)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    # The 'forward' or 'another_forward' of 'TranslatedLayer' cannot be obtained
    # through 'func_name'. So set the special function name '__i_m_p_l__'.
    if hasattr(module, '__i_m_p_l__'):
        callable_func = module.__i_m_p_l__
        callable_func.__name__ = func_name
    elif hasattr(module, func_name):
        callable_func = getattr(module, func_name)
    else:
        raise ValueError(
            'Function: %s doesn\'t exist in the Module transformed from AST.'
            % func_name
        )
    # After transform dygraph function into callable_func saved in tmp file,
    # it lost the global variables from imported statements or defined in source file.
    # Recovers the necessary variables by `__globals__`.
    recover_globals_attribute(dyfunc, callable_func)

    return callable_func, f.name


def _inject_import_statements():
    import_statements = [
        "import paddle",
        "from paddle import Tensor",
        "import paddle.base as base",
        "import paddle.jit.dy2static as _jst",
        "from typing import *",
        "import numpy as np",
        "import warnings",
        "warnings.filterwarnings('ignore', category=DeprecationWarning)",
    ]
    return '\n'.join(import_statements) + '\n'


def recover_globals_attribute(src_obj, dst_obj):
    attr_name = '__globals__'

    src_globals = getattr(src_obj, attr_name, {})
    dst_globals = getattr(dst_obj, attr_name, {})

    for k, v in src_globals.items():
        # ignore builtin attribute.
        if not (k.startswith('__') and k.endswith('__')):
            dst_globals[k] = v


def func_to_source_code(function, dedent=True):
    """
    Transforms function into raw string of source code.
    """
    if isinstance(function, functools.partial):
        function = function.func
    if not (inspect.isfunction(function) or inspect.ismethod(function)):
        raise TypeError(
            "The type of 'function' should be a function or method, but received {}.".format(
                type(function).__name__
            )
        )

    source_code_list, _ = inspect.getsourcelines(function)
    # Replace comments with blank lines so that error messages are not misplaced
    source_code_list = [
        line if not line.lstrip().startswith('#') else '\n'
        for line in source_code_list
    ]
    source_code = ''.join(source_code_list)

    if dedent:
        source_code = textwrap.dedent(source_code)

    return source_code


def input_specs_compatible(src_input_specs, desired_input_specs):
    """
    Returns True if the two input specs are compatible, otherwise False.

    args:
        src_input_spec (list or tuple[InputSpec et.al]): list/tuple of
            paddle.static.InputSpec or int/str et.al
        desired_input_specs (list or tuple[InputSpec et.al]): list/tuple of
            paddle.static.InputSpec or int/str et.al
    """
    len_specs = len(src_input_specs)
    if len_specs != len(desired_input_specs):
        # NOTE(chenweihang): if the input_spec of jit.save is a subset of
        # input_spec of to_static, also compatible
        for spec in src_input_specs:
            if spec not in desired_input_specs:
                return False
    else:
        for src_spec, desired_spec in zip(src_input_specs, desired_input_specs):
            if isinstance(src_spec, paddle.static.InputSpec) or isinstance(
                desired_spec, paddle.static.InputSpec
            ):
                if not _compatible_tensor_spec(src_spec, desired_spec):
                    return False
            else:
                if not _compatible_non_tensor_spec(src_spec, desired_spec):
                    return False

    return True


def _compatible_tensor_spec(src_spec, desired_spec):
    """
    Check whether two tensor type spec is compatible.
    """
    for spec in [src_spec, desired_spec]:
        if not isinstance(spec, paddle.static.InputSpec):
            return False
    src_shape = src_spec.shape
    other_shape = desired_spec.shape
    len_shape = len(src_shape)
    if len_shape != len(other_shape):
        return False
    for j in range(len_shape):
        if src_shape[j] is None or src_shape[j] < 0:
            continue
        if other_shape[j] is None or other_shape[j] < 0:
            continue
        if src_shape[j] != other_shape[j]:
            return False

    src_dtype = convert_dtype(src_spec.dtype)
    other_dtype = convert_dtype(desired_spec.dtype)
    if src_dtype != other_dtype:
        return False

    return True


def _compatible_non_tensor_spec(src_spec, desired_spec):
    """
    Check whether two non-tensor type spec is compatible.
    """

    def hash_value(spec):
        try:
            hash_val = make_hashable(spec)
        except:
            hash_val = None
        return hash_val

    src_hash_val = hash_value(src_spec)
    desired_hash_val = hash_value(desired_spec)

    if src_hash_val != desired_hash_val:
        return False
    else:
        return True


class NameScope:
    def __init__(self):
        """
        A NameScope is a object which manager all the variable names.
        only FunctionDef and Controlflow node will have a namescope property.

        type can be "function" and "controlflow"

        we don't analyze the read only variable because they don't affect the analysis.
        """
        self.globals = set()
        self.nonlocals = set()
        self.args = set()
        self.father = None  # point to the nearest function name scope.
        self.w_vars = set()  # all qualified + normal names been stored
        self.created = set()  # useful for control flow compatibility
        # only valid in control_flow nodes
        # may be remove later.
        self.push_pop_vars = set()  # we call push and pop in the vars

    def set_father(self, father):
        self.father = father

    def existed_vars(self):
        """vars existing in current scope.
        they must not contain qualified names.
        """
        local_vars = self.w_vars - self.globals - self.nonlocals - self.args
        return set(filter(lambda x: '.' not in x, local_vars))

    def created_vars(self):
        return self.created

    def modified_vars(self):
        # may be globals / non-locals / args / qualified names and created_vars
        return self.w_vars

    def variadic_length_vars(self):
        """
        At present, we do not support global append, such as

        import numpy as np
        a = []
        def func():
            a.append() # global names `a`, we will raise a warning.
            p.append(a, 1) # global names `np`, we will raise a warning.
        """
        non_global_push_pop_names = []
        for var in self.push_pop_vars:
            if self._is_simple_name(var) and self.is_global_var(var):
                warnings.warn(
                    f"Find variable `{var}` defined in global scope"
                    f" and call `{var}.append() or {var}.pop()`"
                    f", which will be ignored and never be transfered into"
                    f" tensor array."
                )
            else:
                non_global_push_pop_names.append(var)
        return set(non_global_push_pop_names)

    def control_flow_vars(self):
        valid_names = self.w_vars
        tmp = (self.father.global_vars & valid_names,)
        return {"global": tmp, "nonlocal": self.w_vars - tmp}

    def _is_simple_name(self, name):
        if '.' in name or '[' in name:
            return False
        return True

    def is_global_var(self, name):
        """
        Return whether the name is a var created in global scope.
        Search from bottom to top. If it is not created or modified,
        it means global vars; otherwise, it means local vars.
        Only valid after FunctionNameLivenessAnalysis visitor.
        """
        assert self._is_simple_name(
            name
        ), "is_global_var accept a simple name, but get `{name}`."
        ancestor = self
        while ancestor is not None:
            if name in ancestor.globals:
                return True
            if name in (ancestor.nonlocals | ancestor.w_vars):
                return False
            ancestor = ancestor.father
        return True

    def is_local_var(self, name):
        return not self.is_global_var(name)

    def merge_from(self, name_scope):
        self.globals |= name_scope.globals
        self.nonlocals |= name_scope.nonlocals
        self.args |= name_scope.args
        self.w_vars |= name_scope.w_vars
        self.push_pop_vars |= name_scope.push_pop_vars


class FunctionNameLivenessAnalysis(gast.NodeVisitor):
    """analyze the liveness of a function.

    every variables stored in this scope will be collected,
    in addition with global/nonlocal information and
    push_pop information.

    1. global variable is stored in node.var_globals.
    2. nonlocal variable is stored in node.var_nonlocals.
    3. arguments is stored in node.var_args.
    4. if a variable's push and pop attribute is called,
       it will be collected in push_pop_vars. They are
       used for transformation to tensor_array.
       NOTE: push_pop_vars **may not** in w_vars.
       a.push(0) don't modify the variable a, but the content
       of a.

    For example:

    def func(*args, **kargs):
        a = 12
        global i,j
        nonlocal x,y
        print(a)
        i = k
        b = []
        c = [1,2,3]
        for m in range(10):
            q = 12
            b.push(1)
            c.pop()

    After this visitor we have:
    # node is the FunctionDef node with name: "func"
    node.pd_scope = NameScope(
        globals = ['i', 'j'],
        nonlocals = ['x', 'y'],
        args = ['args', 'kargs'],
        wr_vars = ['a', 'i', 'q', 'm', 'c', 'b']
        push_pop_vars = ['b', 'c']
    )
    """

    def __init__(self, root_node):
        self.scope_node_stack = []  # controlflow, functiondef node
        self.visit(root_node)

    def _reset_name_scope(self, node):
        # always reset the node as empty namescope.
        node.pd_scope = NameScope()

    def _get_name_scope(self, node):
        if not hasattr(node, "pd_scope"):
            node.pd_scope = NameScope()
        return node.pd_scope

    def _current_name_scope(self):
        return self._get_name_scope(self.scope_node_stack[-1])

    def _father_name_scope(self):
        if len(self.scope_node_stack) == 1:
            return None
        return self._get_name_scope(self.scope_node_stack[-2])

    def _nearest_function_scope(self):
        if len(self.scope_node_stack) == 1:
            return None
        for node in self.scope_node_stack[-2::-1]:
            if isinstance(node, gast.FunctionDef):
                return self._get_name_scope(node)

    def visit_ListComp(self, node):
        """[ i for i in range(10) ]
        In this case, `i` will not created in FunctionScope.
        We don't collect `i` by not calling generic_visit.
        """
        pass

    def visit_DictComp(self, node):
        """the same as ListComp."""
        pass

    def visit_Name(self, node):
        self.generic_visit(node)
        write_context = (gast.Store, gast.AugStore, gast.Del)
        if isinstance(node.ctx, write_context):
            self._current_name_scope().w_vars.add(node.id)

    def visit_FunctionDef(self, node):
        def pre_func():
            self._current_name_scope().args |= set(
                self._get_argument_names(node)
            )

        def post_func():
            """NOTE: why we need merge w_vars and push_pop_vars here ?
            because we do ifelse_transformer after loop_transformer. Loops will changed into functioons. but we know this function will be called in if. so we add w_vars to father function scope.
            """
            control_flow_function_def = [
                WHILE_BODY_PREFIX,
                WHILE_BODY_PREFIX,
                FOR_CONDITION_PREFIX,
                FOR_BODY_PREFIX,
                TRUE_FUNC_PREFIX,
                FALSE_FUNC_PREFIX,
            ]

            def is_control_flow_def_node():
                for prefix in control_flow_function_def:
                    if node.name.startswith(prefix):
                        return True
                return False

            if self._father_name_scope() and is_control_flow_def_node():
                self._father_name_scope().w_vars |= (
                    self._current_name_scope().w_vars
                )
                self._father_name_scope().push_pop_vars |= (
                    self._current_name_scope().push_pop_vars
                )

        self._visit_scope_node(node, pre_func, post_func)

    def _visit_scope_node(self, node, pre_func, post_func):
        """scope node main visit logic.
        pre_func and post_func is callbacks
        """
        self._reset_name_scope(node)
        self.scope_node_stack.append(node)
        self._current_name_scope().set_father(self._nearest_function_scope())
        if pre_func:
            pre_func()
        self.generic_visit(node)
        if post_func:
            post_func()
        self.scope_node_stack.pop()

    def _visit_controlflow_node(self, node):
        def post_func():
            self._father_name_scope().merge_from(self._current_name_scope())
            self._nearest_function_scope().merge_from(
                self._current_name_scope()
            )
            self._current_name_scope().created = (
                self._nearest_function_scope().existed_vars()
                - node.before_created
            )
            # gather created vars into father and used in CreateUndefinedVarTransform
            self._nearest_function_scope().created |= (
                self._current_name_scope().created
            )

        def pre_func():
            node.before_created = self._nearest_function_scope().existed_vars()

        self._visit_scope_node(node, pre_func, post_func)

    def visit_For(self, node):
        self._visit_controlflow_node(node)

    def visit_While(self, node):
        self._visit_controlflow_node(node)

    def visit_If(self, node):
        self._visit_controlflow_node(node)

    def visit_Global(self, node):
        self._current_name_scope().globals |= set(node.names)

    def visit_Nonlocal(self, node):
        self._current_name_scope().nonlocals |= set(node.names)

    def visit_Attribute(self, node):
        self.generic_visit(node)
        write_context = (gast.Store, gast.AugStore, gast.Del)
        if isinstance(node.ctx, write_context):
            name = ast_to_source_code(node).strip()
            self._current_name_scope().w_vars.add(name)

    def visit_Subscript(self, node):
        self.generic_visit(node)
        write_context = (gast.Store, gast.AugStore, gast.Del)
        if isinstance(node.ctx, write_context):
            while isinstance(node.value, gast.Subscript):
                node = node.value
            if isinstance(node.value, gast.Name):
                self._current_name_scope().w_vars.add(node.value.id)

    def visit_Call(self, node):
        self.generic_visit(node)
        if not isinstance(node.func, gast.Attribute):
            return
        variadic_length_method = ['append', 'pop']
        if node.func.attr not in variadic_length_method:
            return
        # we don't treat push and pop as a write operator. such as a[i]=10 is not modify a.
        name = ast_to_source_code(node.func.value).strip()
        self._current_name_scope().push_pop_vars.add(name)

    def _get_argument_names(self, node):
        """get all arguments name in the functiondef node.
        this node is local to the function and shouldn't
        be created.
        """
        assert isinstance(
            node, gast.FunctionDef
        ), "Input node is not function define node"
        names = list(node.args.args)
        names.append(node.args.vararg)
        names.append(node.args.kwarg)
        names = [i.id for i in names if i is not None]
        return names


def create_get_args_node(names):
    """
    Create get_args function as follows:

        def get_args_0():
            nonlocal x, y
            return x, y
    """

    def empty_node():
        func_def = f"""
        def {unique_name.generate(GET_ARGS_FUNC_PREFIX)}():
            return
        """
        return gast.parse(textwrap.dedent(func_def)).body[0]

    assert isinstance(names, (list, tuple))
    node = create_nonlocal_stmt_nodes(names)
    if not names:
        return empty_node()
    if node == []:
        nonlocal_vars = "\n"
    else:
        nonlocal_vars = ast_to_source_code(node[0])
    template = """
    def {func_name}():
        {nonlocal_vars}
        return {vars},
    """
    func_def = template.format(
        func_name=unique_name.generate(GET_ARGS_FUNC_PREFIX),
        nonlocal_vars=nonlocal_vars,
        vars=",".join(names),
    )
    return gast.parse(textwrap.dedent(func_def)).body[0]


def create_set_args_node(names):
    """
    Create set_args function as follows:

        def set_args_0(__args):
            nonlocal x, y
            x, y = __args
    """

    def empty_node():
        func_def = f"""
        def {unique_name.generate(SET_ARGS_FUNC_PREFIX)}({ARGS_NAME}):
            pass
        """
        return gast.parse(textwrap.dedent(func_def)).body[0]

    assert isinstance(names, (list, tuple))
    node = create_nonlocal_stmt_nodes(names)
    if not names:
        return empty_node()
    if node == []:
        nonlocal_vars = "\n"
    else:
        nonlocal_vars = ast_to_source_code(node[0])
    template = """
    def {func_name}({args}):
        {nonlocal_vars}
        {vars}, = {args}
    """
    func_def = template.format(
        func_name=unique_name.generate(SET_ARGS_FUNC_PREFIX),
        args=ARGS_NAME,
        nonlocal_vars=nonlocal_vars,
        vars=",".join(names),
    )
    return gast.parse(textwrap.dedent(func_def)).body[0]


def create_nonlocal_stmt_nodes(names):
    assert isinstance(names, (list, tuple))

    mapped = list(filter(lambda n: '.' not in n, names))
    mapped = list(filter(lambda n: '[' not in n, mapped))
    names = sorted(
        mapped, key=mapped.index
    )  # to keep the order, we can't use set() to unique
    if not names:
        return []
    func_code = "nonlocal {}".format(','.join(names))
    return [gast.parse(func_code).body[0]]


class GetterSetterHelper:
    """we have two classes of names in setter and getter function:
    w_vars(loop_vars) + push_pop_vars
    To simplify the setter logic in convert_while and convert_cond,
    we extract the helper class here.
    """

    def __init__(self, getter_func, setter_func, *name_lists):
        name_lists = ([] if x is None else x for x in name_lists)
        name_sets = (set(x) for x in name_lists)
        self._union = list(
            functools.reduce(lambda x, y: x | y, name_sets, set())
        )
        self._union.sort()
        self.getter = getter_func
        self.setter = setter_func
        self.name2id = {name: idx for idx, name in enumerate(self._union)}

    def union(self):
        return self._union

    def get(self, names):
        if names is None:
            names = []
        vars = self.getter()
        if vars is None:
            return ()
        for n in names:
            assert (
                n in self.name2id
            ), f"the name `{n}` not in name union set`{self.name2id.keys()}`."
        return tuple(vars[self.name2id[n]] for n in names)

    def set(self, names, values):
        if names is None:
            names = []
        if values is None:
            values = []
        vars = self.getter()
        if vars is None:
            return
        for n in names:
            assert (
                n in self.name2id
            ), f"the name `{n}` not in name union set`{self.name2id.keys()}`."
        vars = list(vars)
        indices = [self.name2id[n] for n in names]
        for i, v in zip(indices, values):
            vars[i] = v
        self.setter(vars)


def create_name_str(name_ids):
    """
    Return "('x', 'y')" for [x, y]
    """
    if not name_ids:
        return 'None'

    names_str = ["'%s'" % (name.replace("'", "\\'")) for name in name_ids]
    return "(%s, )" % ','.join(names_str)


def prim_or_cinn_is_enabled(build_strategy, backend):
    return cinn_is_enabled(build_strategy, backend) or prim_is_enabled()


def cinn_is_enabled(build_strategy, backend):
    if backend == 'CINN':
        return True
    if build_strategy is not None and build_strategy.build_cinn_pass:
        return True

    value = os.getenv('FLAGS_use_cinn')
    if value is not None and value.lower() in ['true', '1']:
        return True
    return False


def prim_is_enabled():
    core.check_and_set_prim_all_enabled()
    return core._is_bwd_prim_enabled() or core._is_fwd_prim_enabled()


def is_builtin(func, name=None):
    """predict whether a function is a builtin function with name={name}.
    if name == None, then any builtin function will return True
    """

    def name_judge():
        return name is None or func.__name__ == name

    if isinstance(func, types.BuiltinFunctionType) and name_judge():
        return True
    elif func in builtins.__dict__.values() and name_judge():
        return True
    else:
        return False


@signature_safe_contextmanager
def backend_guard(backend):
    core.check_and_set_prim_all_enabled()
    orign_fwd = core._is_fwd_prim_enabled()
    orign_bwd = core._is_bwd_prim_enabled()

    if backend == 'CINN':
        core._set_prim_all_enabled(True)
    try:
        yield
    finally:
        core._set_prim_forward_enabled(orign_fwd)
        core._set_prim_backward_enabled(orign_bwd)


def construct_grad_names(grad_info_map, x_vars, param_vars, out_vars):
    grad_var_names = {}
    fn = (
        lambda grad_var: grad_var.name
        if isinstance(grad_var, framework.Variable)
        else framework.EMPTY_VAR_NAME
    )
    x_grad_vars = backward._get_grad_vars(grad_info_map, x_vars)
    grad_var_names['x'] = list(map(fn, x_grad_vars))
    param_grad_vars = backward._get_grad_vars(grad_info_map, param_vars)
    grad_var_names['param'] = list(map(fn, param_grad_vars))
    out_grad_vars = backward._get_grad_vars(grad_info_map, out_vars)
    grad_var_names['out'] = list(map(fn, out_grad_vars))
    return grad_var_names


@signature_safe_contextmanager
def tensor_name_guard(tensors, names):
    try:
        assert len(tensors) == len(names)
        origin_names = [t.name for t in tensors]
        for t, name in zip(tensors, names):
            t.name = name
        yield
    finally:
        for t, name in zip(tensors, origin_names):
            t.name = name


def cuda_pinned_tensors_move_to_excepted_place(inputs):
    if paddle.is_compiled_with_cuda():
        expected_place = framework._current_expected_place()
        cuda_pinned_place = CUDAPinnedPlace()

        for value in flatten(inputs):
            if (
                isinstance(value, core.eager.Tensor)
                and value.stop_gradient
                and value.place._equals(cuda_pinned_place)
            ):
                var = value._copy_to(expected_place, True)
                var.stop_gradient = True
                var._share_buffer_to(value)
