// Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
#include "paddle/phi/kernels/view_grad_kernel.h"
#include "paddle/phi/backends/all_context.h"
#include "paddle/phi/core/kernel_registry.h"
#include "paddle/phi/kernels/view_kernel.h"

namespace phi {

template <typename Context>
void ViewShapeGradKernel(const Context& dev_ctx,
                         const DenseTensor& input,
                         const DenseTensor& out_grad,
                         const std::vector<int64_t>& dims,
                         DenseTensor* input_grad) {
  ViewShapeKernel<Context>(
      dev_ctx, out_grad, common::vectorize<int64_t>(input.dims()), input_grad);
}

template <typename Context>
void ViewDtypeGradKernel(const Context& dev_ctx,
                         const DenseTensor& input,
                         const DenseTensor& out_grad,
                         DataType dtype,
                         DenseTensor* input_grad) {
  ViewDtypeKernel<Context>(dev_ctx, out_grad, input.dtype(), input_grad);
}
}  // namespace phi

PD_REGISTER_KERNEL_FOR_ALL_BACKEND_DTYPE_EXCEPT_CUSTOM(
    view_grad_shape, STRIDED, phi::ViewShapeGradKernel) {}

PD_REGISTER_KERNEL_FOR_ALL_BACKEND_DTYPE_EXCEPT_CUSTOM(
    view_grad_dtype, STRIDED, phi::ViewDtypeGradKernel) {}
