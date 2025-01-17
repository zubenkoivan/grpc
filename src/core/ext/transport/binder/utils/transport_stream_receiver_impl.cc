// Copyright 2021 gRPC authors.
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

#include <grpc/impl/codegen/port_platform.h>

#include "src/core/ext/transport/binder/utils/transport_stream_receiver_impl.h"

#include <grpc/support/log.h>

#include <functional>
#include <string>
#include <utility>

namespace grpc_binder {

const absl::string_view
    TransportStreamReceiver::kGrpcBinderTransportCancelledGracefully =
        "grpc-binder-transport: cancelled gracefully";

void TransportStreamReceiverImpl::RegisterRecvInitialMetadata(
    StreamIdentifier id, InitialMetadataCallbackType cb) {
  gpr_log(GPR_INFO, "%s id = %d is_client = %d", __func__, id, is_client_);
  GPR_ASSERT(initial_metadata_cbs_.count(id) == 0);
  absl::StatusOr<Metadata> initial_metadata{};
  {
    grpc_core::MutexLock l(&m_);
    auto iter = pending_initial_metadata_.find(id);
    if (iter == pending_initial_metadata_.end()) {
      initial_metadata_cbs_[id] = std::move(cb);
      cb = nullptr;
    } else {
      initial_metadata = std::move(iter->second.front());
      iter->second.pop();
      if (iter->second.empty()) {
        pending_initial_metadata_.erase(iter);
      }
    }
  }
  if (cb != nullptr) {
    cb(std::move(initial_metadata));
  }
}

void TransportStreamReceiverImpl::RegisterRecvMessage(
    StreamIdentifier id, MessageDataCallbackType cb) {
  gpr_log(GPR_INFO, "%s id = %d is_client = %d", __func__, id, is_client_);
  GPR_ASSERT(message_cbs_.count(id) == 0);
  absl::StatusOr<std::string> message{};
  {
    grpc_core::MutexLock l(&m_);
    auto iter = pending_message_.find(id);
    if (iter == pending_message_.end()) {
      // If we'd already received trailing-metadata and there's no pending
      // messages, cancel the callback.
      if (recv_message_cancelled_.count(id)) {
        cb(absl::CancelledError(
            TransportStreamReceiver::kGrpcBinderTransportCancelledGracefully));
      } else {
        message_cbs_[id] = std::move(cb);
      }
      cb = nullptr;
    } else {
      // We'll still keep all pending messages received before the trailing
      // metadata since they're issued before the end of stream, as promised by
      // WireReader which keeps transactions commit in-order.
      message = std::move(iter->second.front());
      iter->second.pop();
      if (iter->second.empty()) {
        pending_message_.erase(iter);
      }
    }
  }
  if (cb != nullptr) {
    cb(std::move(message));
  }
}

void TransportStreamReceiverImpl::RegisterRecvTrailingMetadata(
    StreamIdentifier id, TrailingMetadataCallbackType cb) {
  gpr_log(GPR_INFO, "%s id = %d is_client = %d", __func__, id, is_client_);
  GPR_ASSERT(trailing_metadata_cbs_.count(id) == 0);
  std::pair<absl::StatusOr<Metadata>, int> trailing_metadata{};
  {
    grpc_core::MutexLock l(&m_);
    auto iter = pending_trailing_metadata_.find(id);
    if (iter == pending_trailing_metadata_.end()) {
      trailing_metadata_cbs_[id] = std::move(cb);
      cb = nullptr;
    } else {
      trailing_metadata = std::move(iter->second.front());
      iter->second.pop();
      if (iter->second.empty()) {
        pending_trailing_metadata_.erase(iter);
      }
    }
  }
  if (cb != nullptr) {
    cb(std::move(trailing_metadata.first), trailing_metadata.second);
  }
}

void TransportStreamReceiverImpl::NotifyRecvInitialMetadata(
    StreamIdentifier id, absl::StatusOr<Metadata> initial_metadata) {
  gpr_log(GPR_INFO, "%s id = %d is_client = %d", __func__, id, is_client_);
  if (!is_client_ && accept_stream_callback_) {
    accept_stream_callback_();
  }
  InitialMetadataCallbackType cb;
  {
    grpc_core::MutexLock l(&m_);
    auto iter = initial_metadata_cbs_.find(id);
    if (iter != initial_metadata_cbs_.end()) {
      cb = iter->second;
      initial_metadata_cbs_.erase(iter);
    } else {
      pending_initial_metadata_[id].push(std::move(initial_metadata));
      return;
    }
  }
  cb(std::move(initial_metadata));
}

void TransportStreamReceiverImpl::NotifyRecvMessage(
    StreamIdentifier id, absl::StatusOr<std::string> message) {
  gpr_log(GPR_INFO, "%s id = %d is_client = %d", __func__, id, is_client_);
  MessageDataCallbackType cb;
  {
    grpc_core::MutexLock l(&m_);
    auto iter = message_cbs_.find(id);
    if (iter != message_cbs_.end()) {
      cb = iter->second;
      message_cbs_.erase(iter);
    } else {
      pending_message_[id].push(std::move(message));
      return;
    }
  }
  cb(std::move(message));
}

void TransportStreamReceiverImpl::NotifyRecvTrailingMetadata(
    StreamIdentifier id, absl::StatusOr<Metadata> trailing_metadata,
    int status) {
  // Trailing metadata mark the end of the stream. Since TransportStreamReceiver
  // assumes in-order commitments of transactions and that trailing metadata is
  // parsed after message data, we can safely cancel all upcoming callbacks of
  // recv_message.
  gpr_log(GPR_INFO, "%s id = %d is_client = %d", __func__, id, is_client_);
  CancelRecvMessageCallbacksDueToTrailingMetadata(id);
  TrailingMetadataCallbackType cb;
  {
    grpc_core::MutexLock l(&m_);
    auto iter = trailing_metadata_cbs_.find(id);
    if (iter != trailing_metadata_cbs_.end()) {
      cb = iter->second;
      trailing_metadata_cbs_.erase(iter);
    } else {
      pending_trailing_metadata_[id].emplace(std::move(trailing_metadata),
                                             status);
      return;
    }
  }
  cb(std::move(trailing_metadata), status);
}

void TransportStreamReceiverImpl::
    CancelRecvMessageCallbacksDueToTrailingMetadata(StreamIdentifier id) {
  gpr_log(GPR_INFO, "%s id = %d is_client = %d", __func__, id, is_client_);
  MessageDataCallbackType cb = nullptr;
  {
    grpc_core::MutexLock l(&m_);
    auto iter = message_cbs_.find(id);
    if (iter != message_cbs_.end()) {
      cb = std::move(iter->second);
      message_cbs_.erase(iter);
    }
    recv_message_cancelled_.insert(id);
  }
  if (cb != nullptr) {
    // The registered callback will never be satisfied. Cancel it.
    cb(absl::CancelledError(
        TransportStreamReceiver::kGrpcBinderTransportCancelledGracefully));
  }
}

void TransportStreamReceiverImpl::CancelStream(StreamIdentifier id,
                                               absl::Status error) {
  InitialMetadataCallbackType initial_metadata_callback = nullptr;
  MessageDataCallbackType message_data_callback = nullptr;
  TrailingMetadataCallbackType trailing_metadata_callback = nullptr;
  {
    grpc_core::MutexLock l(&m_);
    auto initial_metadata_iter = initial_metadata_cbs_.find(id);
    if (initial_metadata_iter != initial_metadata_cbs_.end()) {
      initial_metadata_callback = std::move(initial_metadata_iter->second);
      initial_metadata_cbs_.erase(initial_metadata_iter);
    }
    auto message_data_iter = message_cbs_.find(id);
    if (message_data_iter != message_cbs_.end()) {
      message_data_callback = std::move(message_data_iter->second);
      message_cbs_.erase(message_data_iter);
    }
    auto trailing_metadata_iter = trailing_metadata_cbs_.find(id);
    if (trailing_metadata_iter != trailing_metadata_cbs_.end()) {
      trailing_metadata_callback = std::move(trailing_metadata_iter->second);
      trailing_metadata_cbs_.erase(trailing_metadata_iter);
    }
  }
  if (initial_metadata_callback != nullptr) {
    initial_metadata_callback(error);
  }
  if (message_data_callback != nullptr) {
    message_data_callback(error);
  }
  if (trailing_metadata_callback != nullptr) {
    trailing_metadata_callback(error, 0);
  }
}

void TransportStreamReceiverImpl::Clear(StreamIdentifier id) {
  gpr_log(GPR_INFO, "%s id = %d is_client = %d", __func__, id, is_client_);
  grpc_core::MutexLock l(&m_);
  initial_metadata_cbs_.erase(id);
  message_cbs_.erase(id);
  trailing_metadata_cbs_.erase(id);
  recv_message_cancelled_.erase(id);
  pending_initial_metadata_.erase(id);
  pending_message_.erase(id);
  pending_trailing_metadata_.erase(id);
}
}  // namespace grpc_binder
