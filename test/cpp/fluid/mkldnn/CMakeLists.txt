get_property(paddle_lib GLOBAL PROPERTY PADDLE_LIB_NAME)
cc_test(
  test_mkldnn_op_inplace
  SRCS test_mkldnn_op_inplace.cc
  DEPS executor
       op_registry
       elementwise_add_op
       activation_op
       phi
       common
       scope
       device_context
       enforce
       generated_static_op)
cc_test(
  test_mkldnn_cpu_quantize_pass
  SRCS test_mkldnn_cpu_quantize_pass.cc
  DEPS executor
       op_registry
       activation_op
       conv_activation_mkldnn_fuse_pass
       cpu_quantize_placement_pass
       cpu_quantize_pass
       phi
       common
       scope
       device_context)

cc_test(
  test_conv_mkldnn_nhwc
  SRCS test_conv_mkldnn_nhwc.cc
  DEPS executor
       op_registry
       depthwise_conv
       tensor
       phi
       common
       scope
       device_context
       enforce
       generated_static_op)

set(TEST_MKLDNN_CACHING_DEPS
    op_registry
    elementwise_mul_op
    elementwise_add_op
    activation_op
    phi
    common
    scope
    device_context
    enforce
    generated_static_op)

if(WITH_GPU OR WITH_ROCM)
  set(TEST_MKLDNN_CACHING_DEPS ${TEST_MKLDNN_CACHING_DEPS} depthwise_conv)
endif()
cc_test(
  test_mkldnn_caching
  SRCS test_mkldnn_caching.cc
  DEPS ${TEST_MKLDNN_CACHING_DEPS})

if(WITH_TESTING)
  paddle_test(test_mkldnn_op_nhwc SRCS test_mkldnn_op_nhwc.cc)
endif()

cc_test(
  test_mkldnn_pool_adaptive_op
  SRCS test_mkldnn_pool_adaptive_op.cc
  DEPS fleet_executor
       conditional_block_op
       executor
       op_registry
       generated_static_op
       generated_op
       phi
       common
       scope
       device_context
       enforce)

cc_test(
  test_mkldnn_squeeze
  SRCS test_mkldnn_squeeze.cc
  DEPS fleet_executor
       conditional_block_op
       executor
       op_registry
       generated_static_op
       generated_op
       phi
       scope
       device_context
       enforce)

if(WITH_ONNXRUNTIME AND WIN32)
  # Copy onnxruntime for some c++ test in Windows, since the test will
  # be build only in CI, so suppose the generator in Windows is Ninja.
  copy_onnx(test_mkldnn_op_nhwc)
endif()
