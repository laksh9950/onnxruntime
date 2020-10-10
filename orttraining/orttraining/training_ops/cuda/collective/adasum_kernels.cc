// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

#include "orttraining/training_ops/cuda/collective/adasum_kernels.h"
#include "orttraining/training_ops/cpu/controlflow/common.h"
#include "orttraining/core/framework/communication/mpi/mpi_context.h"

namespace onnxruntime {
namespace cuda {

Status AdasumAllReduce::ComputeInternal(OpKernelContext* context) const {

  int vhdd_start_level = 1;
  if (adasum_reduce_algo_ == training::AdasumReductionType::GpuHierarchical) {
    vhdd_start_level = training::DistributedRunContext::GetInstance().GroupSize(training::WorkerGroupType::NodeLocalDataParallel);
  }
  // Get tensor count
  const int num_tensors = context->InputCount();
  std::vector<int> tensor_element_counts;
  std::vector<size_t> tensor_offsets;
  std::vector<size_t> tensor_sizes;

  int64_t total_recv_buffer_len = 0;

  size_t size_in_bytes = 0;
  for (int i = 0; i < num_tensors; ++i) {
    const Tensor* x_tensor = context->Input<Tensor>(i);
    tensor_offsets.push_back(size_in_bytes);

    size_in_bytes = x_tensor->SizeInBytes();
    total_recv_buffer_len += size_in_bytes;

    tensor_sizes.push_back(size_in_bytes);
    tensor_element_counts.push_back((int)x_tensor->Shape().Size());
  }

  // Allocate temp scratch buffer in cpu space.
  AllocatorPtr allocator;
  allocator = Info().GetAllocator(0, OrtMemTypeCPU);
  auto data_buffer = allocator->Alloc(total_recv_buffer_len);
  BufferUniquePtr data_buffer_ptr(data_buffer, BufferDeleter(allocator));

  for (int i = 0; i < num_tensors; ++i) {
    const Tensor* x_tensor = context->Input<Tensor>(i);
    CUDA_CALL(cudaMemcpy((uint8_t*)data_buffer_ptr.get() + tensor_offsets[i], x_tensor->DataRaw(),
                      tensor_sizes[i], cudaMemcpyDeviceToHost));
  }

  auto recv_buffer = allocator->Alloc(total_recv_buffer_len);
  BufferUniquePtr recv_buffer_ptr(recv_buffer, BufferDeleter(allocator));

  ORT_RETURN_IF_ERROR(adasum_reducer_->DispatchFusedAllreduce((void*)data_buffer, recv_buffer, tensor_element_counts,
                          vhdd_start_level, // start level
                          training::MPIContext::GetInstance().GetMPIGroup(training::WorkerGroupType::GlobalParallel).communicator, // communicator
                          0, // tag
                          adasum_reducer_->GetReductionComms(), // reduction_comms
                          context->Input<Tensor>(0)->DataType()));

  for (int i = 0; i < num_tensors; i++) {
    Tensor* y_tensor = context->Output(i, context->Input<Tensor>(i)->Shape());
    CUDA_CALL(cudaMemcpy(y_tensor->MutableDataRaw(), (uint8_t*)data_buffer + tensor_offsets[i],
                      tensor_sizes[i], cudaMemcpyHostToDevice));
  }
  return Status::OK();
}

ONNX_OPERATOR_KERNEL_EX(
    AdasumAllReduce,
    kMSDomain,
    1,
    kCudaExecutionProvider,
    KernelDefBuilder()
        .Alias(onnxruntime::contrib::AliasRange<0, 0>(0, 1024))
        .TypeConstraint("T", DataTypeImpl::AllIEEEFloatTensorTypes()),
    AdasumAllReduce);

}  // namespace cuda
}  // namespace onnxruntime
