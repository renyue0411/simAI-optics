/* 
*Copyright (c) 2024, Alibaba Group;
*Licensed under the Apache License, Version 2.0 (the "License");
*you may not use this file except in compliance with the License.
*You may obtain a copy of the License at

*   http://www.apache.org/licenses/LICENSE-2.0

*Unless required by applicable law or agreed to in writing, software
*distributed under the License is distributed on an "AS IS" BASIS,
*WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*See the License for the specific language governing permissions and
*limitations under the License.
*/

#ifndef __ENTRY_H__
#define __ENTRY_H__

#undef PGO_TRAINING
#define PATH_TO_PGO_CONFIG "path_to_pgo_config"
#define _QPS_PER_CONNECTION_  1
#include <cstdlib>
#include <deque>
#include <limits>
#include <stdexcept>
#include <tuple>
#include "common.h"
#include "ns3/applications-module.h"
#include "ns3/core-module.h"
#include "ns3/error-model.h"
#include "ns3/global-route-manager.h"
#include "ns3/internet-module.h"
#include "ns3/ipv4-static-routing-helper.h"
#include "ns3/packet.h"
#include "ns3/point-to-point-helper.h"
#include "ns3/qbb-helper.h"
#include <fstream>
#include <iostream>
#include <ns3/rdma-client-helper.h>
#include <ns3/rdma-client.h>
#include <ns3/rdma-driver.h>
#include <ns3/rdma.h>
#include <ns3/sim-setting.h>
#include <ns3/switch-node.h>
#include <time.h>
#include <unordered_map>
#include <mutex>
#include <vector>
#ifdef NS3_MTP
#include "ns3/mtp-interface.h"
#endif
#include <map>
#include"astra-sim/system/MockNcclQps.h"
#include "astra-sim/system/MockNcclLog.h"
using namespace ns3;
using namespace std;


std::map<std::pair<std::pair<int, int>,int>, AstraSim::ncclFlowTag> receiver_pending_queue;


std::map<std::pair<int, std::pair<int, int>>, AstraSim::ncclFlowTag> sender_src_port_map; 
struct task1 {
  int src;
  int dest;
  int type;
  uint64_t count;
  void *fun_arg;
  void (*msg_handler)(void *fun_arg);
  double schTime; 
};
map<std::pair<int, std::pair<int, int>>, struct task1> expeRecvHash;
map<std::pair<int, std::pair<int, int>>, uint64_t> recvHash;
map<std::pair<int, std::pair<int, int>>, struct task1> sentHash;
map<std::pair<int, int>, int64_t> nodeHash;
map<std::pair<int,std::pair<int,int>>,int> waiting_to_sent_callback;  
map<std::pair<int,std::pair<int,int>>,int>waiting_to_notify_receiver;
map<std::pair<int,std::pair<int,int>>,uint64_t>received_chunksize;  
map<std::pair<int,std::pair<int,int>>,uint64_t>sent_chunksize;  
bool is_sending_finished(int src,int dst,AstraSim::ncclFlowTag flowTag){
  int tag_id = flowTag.current_flow_id;
  if (waiting_to_sent_callback.count(
          std::make_pair(tag_id, std::make_pair(src, dst)))) {
    if (--waiting_to_sent_callback[std::make_pair(
            tag_id, std::make_pair(src, dst))] == 0) {
      waiting_to_sent_callback.erase(
          std::make_pair(tag_id, std::make_pair(src, dst)));
      return true;
    }
  }
  return false;
}

bool is_receive_finished(int src,int dst,AstraSim::ncclFlowTag flowTag){
  int tag_id = flowTag.current_flow_id;
  map<std::pair<int,std::pair<int,int>>,int>::iterator it;
  MockNcclLog* NcclLog = MockNcclLog::getInstance();
  if (waiting_to_notify_receiver.count(
          std::make_pair(tag_id, std::make_pair(src, dst)))) {
    NcclLog->writeLog(NcclLogLevel::DEBUG," is_receive_finished waiting_to_notify_receiver  tag_id  %d src  %d dst  %d count  %d",tag_id,src,dst,waiting_to_notify_receiver[std::make_pair(
                     tag_id, std::make_pair(src, dst))]);
    if (--waiting_to_notify_receiver[std::make_pair(
            tag_id, std::make_pair(src, dst))] == 0) {
      waiting_to_notify_receiver.erase(
          std::make_pair(tag_id, std::make_pair(src, dst)));
      return true;
    }
  }
  return false;
}

// Figure 2: one long-lived simulated RC QP per (sender, receiver, channel,
// QP index). The QP's actual UDP source port is stable for its entire life.
// Each NCCL fragment becomes one posted byte range on that QP.
using Figure2Key = std::tuple<int, int, int, int>;
using Figure2WireKey = std::tuple<uint32_t, uint32_t, uint16_t, uint16_t, uint16_t>;
struct Figure2Fragment {
  uint64_t endSeq;
  uint64_t bytes;
  AstraSim::ncclFlowTag flowTag;
};
struct Figure2Qp {
  Ptr<RdmaQueuePair> qp;
  uint64_t reservedBytes = 0;
  std::deque<Figure2Fragment> txPending;
  std::deque<Figure2Fragment> rxPending;
};
std::map<Figure2Key, Figure2Qp> figure2Qps;
std::map<Figure2WireKey, Figure2Key> figure2WireQps;

int GetFigure2QpsPerChannel() {
  static const int qps = []() {
    const char *value = std::getenv("SIMAI_FIG2_QPS_PER_CHANNEL");
    if (value == nullptr) return 0;
    char *end = nullptr;
    const long parsed = std::strtol(value, &end, 10);
    if (*value == '\0' || *end != '\0' ||
        (parsed != 1 && parsed != 2 && parsed != 4 && parsed != 8 && parsed != 16 && parsed != 32)) {
      throw std::invalid_argument(
          "SIMAI_FIG2_QPS_PER_CHANNEL must be 1, 2, 4, 8, 16, or 32");
    }
    return static_cast<int>(parsed);
  }();
  return qps;
}

void Figure2Post(Ptr<RdmaHw> hw, Ptr<RdmaQueuePair> qp,
                 uint64_t expectedStart, uint64_t bytes) {
  NS_ABORT_MSG_IF(qp->GetPostedLimit() != expectedStart,
                  "Figure 2 QP posts were reordered");
  hw->AppendDefaultQpBytes(qp, bytes);
}

void SendPersistentFlow(int src, int dst, uint64_t bytes,
                        AstraSim::sim_request *request) {
  const int qps = GetFigure2QpsPerChannel();
  const int channel = request->flowTag.channel_id;
  NS_ABORT_MSG_IF(channel < 0, "Figure 2 requires a valid channel ID");
  NS_ABORT_MSG_IF(bytes == 0, "Figure 2 requires a nonempty fragment");
  NS_ABORT_MSG_IF(rdma_transport_mode != 0 || cc_mode != 1 ||
                      rc_ack_retry_enable != 0,
                  "Figure 2 baseline requires RDMA_TRANSPORT_MODE=0, "
                  "CC_MODE=1 (DCQCN), RC_ACK_RETRY_ENABLE=0");
  Ptr<RdmaDriver> driver = n.Get(src)->GetObject<RdmaDriver>();
  NS_ABORT_MSG_IF(driver == nullptr || driver->m_injectionMode != 0,
                  "Figure 2 persistent QPs require RDMA_TRANSPORT_MODE=0");
  Ptr<RdmaHw> hw = driver->m_rdma;
  const bool nvls = request->flowTag.nvls_on;
  if (nvls) driver->EnbaleNVLS();
  else driver->DisableNVLS();

  const uint64_t chunk = (bytes + qps - 1) / qps;
  uint64_t remaining = bytes;
  for (int index = 0; index < qps && remaining != 0; ++index) {
    const uint64_t part = std::min(chunk, remaining);
    remaining -= part;
    Figure2Key key(src, dst, channel, index);
    Figure2Qp &state = figure2Qps[key];
    if (state.qp == nullptr) {
      const uint32_t allocatedPort = portNumber[src][dst]++;
      NS_ABORT_MSG_IF(allocatedPort == 0 || allocatedPort > 65535,
                      "Figure 2 UDP source port exhausted");
      const uint16_t sport = static_cast<uint16_t>(allocatedPort);
      // Only the posted prefix is sent. m_size is a non-allocating lifetime
      // upper bound so ACKs for one fragment cannot tear down this QP.
      constexpr uint64_t capacity = 1ULL << 40;
      state.qp = hw->CreateQueuePair(
          src, dst, request->flowTag.current_flow_id, capacity, 3,
          serverAddress[src], serverAddress[dst], sport, 100,
          has_win ? (qp_win_bytes != 0 ? qp_win_bytes :
                     (global_t == 1 ? maxBdp : pairBdp[n.Get(src)][n.Get(dst)])) : 0,
          global_t == 1 ? maxRtt : pairRtt[src][dst],
          Callback<void>(), Callback<void>(), 0);
      const Figure2WireKey wire(serverAddress[src].Get(),
                                serverAddress[dst].Get(), sport, 100, 3);
      NS_ABORT_MSG_IF(figure2WireQps.count(wire) != 0,
                      "Figure 2 QP five-tuple reused");
      figure2WireQps.emplace(wire, key);
      std::cout << "FIG2_QP_CREATE src=" << src << " dst=" << dst
                << " channel=" << channel << " index=" << index
                << " sport=" << sport << std::endl;
    } else {
      NS_ABORT_MSG_IF((state.qp->nvls_enable != 0) != nvls,
                      "Figure 2 channel changed NVLS setting on a live QP");
    }
    const uint64_t start = state.reservedBytes;
    // The legacy SeqTsHeader carries a 32-bit byte sequence number.
    const uint64_t maxSeq = std::numeric_limits<uint32_t>::max() - packet_payload_size;
    NS_ABORT_MSG_IF(start > maxSeq || part > maxSeq - start,
                    "Figure 2 QP exceeded 32-bit byte sequence space");
    state.reservedBytes += part;
    const Figure2Fragment fragment{state.reservedBytes, part, request->flowTag};
    state.txPending.push_back(fragment);
    state.rxPending.push_back(fragment);
    ++flow_input.idx;
    const uint64_t now = Simulator::Now().GetNanoSeconds();
    uint64_t startNs = 6000ULL * 1000;
    if (const char *delay = std::getenv("AS_SEND_LAT")) {
      startNs = static_cast<uint64_t>(std::stoull(delay)) * 1000;
    }
    Simulator::Schedule(NanoSeconds(startNs > now ? startNs - now : 0),
                        &Figure2Post, hw, state.qp, start, part);
    const auto flowKey = std::make_pair(request->flowTag.current_flow_id,
                                        std::make_pair(src, dst));
    ++waiting_to_sent_callback[flowKey];
    ++waiting_to_notify_receiver[flowKey];
  }
}

void SendFlow(int src, int dst, uint64_t maxPacketCount,
              void (*msg_handler)(void *fun_arg), void *fun_arg, int tag, AstraSim::sim_request *request) {
  MockNcclLog*NcclLog = MockNcclLog::getInstance();
  if (std::getenv("SIMAI_FIG2_HASH_GROUPS_PER_CHANNEL") != nullptr) {
    throw std::invalid_argument("Remove SIMAI_FIG2_HASH_GROUPS_PER_CHANNEL; "
                                "use SIMAI_FIG2_QPS_PER_CHANNEL");
  }
  const int persistent = GetFigure2QpsPerChannel();
  if (persistent != 0) {
    static bool printed = false;
    if (!printed) {
      std::cout << "FIG2_QP_CONFIG qps_per_channel=" << persistent
                << " lifetime=whole_run split_each_fragment=1"
                << " rdma_mode=" << rdma_transport_mode
                << " cc_mode=" << cc_mode
                << " rc_ack_retry=" << rc_ack_retry_enable
                << std::endl;
      printed = true;
    }
    SendPersistentFlow(src, dst, maxPacketCount, request);
    return;
  }
  uint64_t PacketCount=((maxPacketCount+_QPS_PER_CONNECTION_-1)/_QPS_PER_CONNECTION_);
  uint64_t leftPacketCount = maxPacketCount;
  for(int index = 0 ;index<_QPS_PER_CONNECTION_;index++){
  uint64_t real_PacketCount = min(PacketCount,leftPacketCount);
  leftPacketCount-=real_PacketCount;
  uint32_t port = portNumber[src][dst]++; 
    {
      #ifdef NS3_MTP
      MtpInterface::explicitCriticalSection cs;
      #endif
      sender_src_port_map[make_pair(port, make_pair(src, dst))] = request->flowTag;
      #ifdef NS3_MTP
      cs.ExitSection();
      #endif
    }
  int flow_id = request->flowTag.current_flow_id;
  bool nvls_on = request->flowTag.nvls_on;
  int pg = 3, dport = 100;
  int send_lat = 6000;
  const char* send_lat_env = std::getenv("AS_SEND_LAT");
  if (send_lat_env) {
    try {
      send_lat = std::stoi(send_lat_env);
    } catch (const std::invalid_argument& e) {
      NcclLog->writeLog(NcclLogLevel::ERROR,"send_lat set error");
      exit(-1);
    }
  }
  send_lat *= 1000;
  flow_input.idx++;
  if(real_PacketCount == 0) real_PacketCount = 1;
    MockNcclLog* NcclLog = MockNcclLog::getInstance();
    NcclLog->writeLog(NcclLogLevel::DEBUG," [Packet sending event]  %dSendFlow to  %d channelid:  %d flow_id  %d srcip  %d dstip  %d size:  %llu at the tick:  %d",src,dst,tag,flow_id,serverAddress[src],serverAddress[dst],maxPacketCount,AstraSim::Sys::boostedTick());
    NcclLog->writeLog(NcclLogLevel::DEBUG," request->flowTag [Packet sending event]  %dSendFlow to  %d tag_id:  %d flow_id  %d srcip  %d dstip  %d size:  %llu at the tick:  %d",request->flowTag.sender_node,request->flowTag.receiver_node,request->flowTag.tag_id,request->flowTag.current_flow_id,serverAddress[src],serverAddress[dst],maxPacketCount,AstraSim::Sys::boostedTick());
  RdmaClientHelper clientHelper(
      pg, serverAddress[src], serverAddress[dst], port, dport, real_PacketCount,
      has_win ? (qp_win_bytes != 0 ? qp_win_bytes :
                 (global_t == 1 ? maxBdp : pairBdp[n.Get(src)][n.Get(dst)])) : 0,
      global_t == 1 ? maxRtt : pairRtt[src][dst], msg_handler, fun_arg, tag,
      src, dst);
  if(nvls_on) clientHelper.SetAttribute("NVLS_enable", UintegerValue (1));
  {
    #ifdef NS3_MTP
    MtpInterface::explicitCriticalSection cs;
    #endif
    ApplicationContainer appCon = clientHelper.Install(n.Get(src));
    appCon.Start(Time(send_lat));
    waiting_to_sent_callback[std::make_pair(request->flowTag.current_flow_id,std::make_pair(src,dst))]++;
    waiting_to_notify_receiver[std::make_pair(request->flowTag.current_flow_id,std::make_pair(src,dst))]++;
    #ifdef NS3_MTP
    cs.ExitSection();
    #endif
  }
  NcclLog->writeLog(NcclLogLevel::DEBUG,"waiting_to_notify_receiver  current_flow_id  %d src  %d dst  %d count  %d",request->flowTag.current_flow_id,src,dst,waiting_to_notify_receiver[std::make_pair(request->flowTag.tag_id,std::make_pair(src,dst))]);
  }
}

void notify_receiver_receive_data(int sender_node, int receiver_node,
                                  uint64_t message_size, AstraSim::ncclFlowTag flowTag) {
  {
    #ifdef NS3_MTP
    MtpInterface::explicitCriticalSection cs;   
    #endif                         
    MockNcclLog* NcclLog = MockNcclLog::getInstance();
    NcclLog->writeLog(NcclLogLevel::DEBUG," %d notify recevier:  %d message size:  %llu",sender_node,receiver_node,message_size);
    int tag = flowTag.tag_id;   
    if (expeRecvHash.find(make_pair(
            tag, make_pair(sender_node, receiver_node))) != expeRecvHash.end()) {
      task1 t2 =
          expeRecvHash[make_pair(tag, make_pair(sender_node, receiver_node))];
    MockNcclLog* NcclLog = MockNcclLog::getInstance();
    NcclLog->writeLog(NcclLogLevel::DEBUG," %d notify recevier:  %d message size:  %llu t2.count:  %llu channle id:  %d",sender_node,receiver_node,message_size,t2.count,flowTag.channel_id);
      AstraSim::RecvPacketEventHadndlerData* ehd = (AstraSim::RecvPacketEventHadndlerData*) t2.fun_arg;
      if (message_size == t2.count) {
        NcclLog->writeLog(NcclLogLevel::DEBUG," message_size = t2.count expeRecvHash.erase  %d notify recevier:  %d message size:  %llu channel_id  %d",sender_node,receiver_node,message_size,tag);
        expeRecvHash.erase(make_pair(tag, make_pair(sender_node, receiver_node)));
        #ifdef NS3_MTP
        cs.ExitSection();
        #endif
        assert(ehd->flowTag.current_flow_id == -1 && ehd->flowTag.child_flow_id == -1);
        ehd->flowTag = flowTag;
        t2.msg_handler(t2.fun_arg);
        goto receiver_end_1st_section;
      } else if (message_size > t2.count) {
        recvHash[make_pair(tag, make_pair(sender_node, receiver_node))] =
            message_size - t2.count;
        NcclLog->writeLog(NcclLogLevel::DEBUG,"message_size > t2.count expeRecvHash.erase %d notify recevier:  %d message size:  %llu channel_id  %d",sender_node,receiver_node,message_size,tag);
        expeRecvHash.erase(make_pair(tag, make_pair(sender_node, receiver_node)));
        #ifdef NS3_MTP
        cs.ExitSection();
        #endif
        assert(ehd->flowTag.current_flow_id == -1 && ehd->flowTag.child_flow_id == -1);
        ehd->flowTag = flowTag;
        t2.msg_handler(t2.fun_arg);
        goto receiver_end_1st_section;
      } else {
        t2.count -= message_size;
        expeRecvHash[make_pair(tag, make_pair(sender_node, receiver_node))] = t2;
      }
    } else {
      receiver_pending_queue[std::make_pair(std::make_pair(receiver_node, sender_node),tag)] = flowTag;
      if (recvHash.find(make_pair(tag, make_pair(sender_node, receiver_node))) ==
          recvHash.end()) {
        recvHash[make_pair(tag, make_pair(sender_node, receiver_node))] =
            message_size;
      } else {
        recvHash[make_pair(tag, make_pair(sender_node, receiver_node))] +=
            message_size;
      }
    }
    #ifdef NS3_MTP
    cs.ExitSection();
    #endif
  receiver_end_1st_section:
    {
    #ifdef NS3_MTP
    MtpInterface::explicitCriticalSection cs2;
    #endif  
    if (nodeHash.find(make_pair(receiver_node, 1)) == nodeHash.end()) {
      nodeHash[make_pair(receiver_node, 1)] = message_size;
    } else {
      nodeHash[make_pair(receiver_node, 1)] += message_size;
    }
    #ifdef NS3_MTP
    cs2.ExitSection();
    #endif
    }
  }
}

void notify_sender_sending_finished(int sender_node, int receiver_node,
                                    uint64_t message_size, AstraSim::ncclFlowTag flowTag) {
  { 
    MockNcclLog * NcclLog = MockNcclLog::getInstance();
    #ifdef NS3_MTP
    MtpInterface::explicitCriticalSection cs;
    #endif    
    int tag = flowTag.tag_id;        
    if (sentHash.find(make_pair(tag, make_pair(sender_node, receiver_node))) !=
      sentHash.end()) {
      task1 t2 = sentHash[make_pair(tag, make_pair(sender_node, receiver_node))];
      AstraSim::SendPacketEventHandlerData* ehd = (AstraSim::SendPacketEventHandlerData*) t2.fun_arg;
      ehd->flowTag=flowTag;   
      if (t2.count == message_size) {
        sentHash.erase(make_pair(tag, make_pair(sender_node, receiver_node)));
        if (nodeHash.find(make_pair(sender_node, 0)) == nodeHash.end()) {
          nodeHash[make_pair(sender_node, 0)] = message_size;
        } else {
          nodeHash[make_pair(sender_node, 0)] += message_size;
        }
        #ifdef NS3_MTP
        cs.ExitSection();
        #endif
        t2.msg_handler(t2.fun_arg);
        goto sender_end_1st_section;
      }else{
        NcclLog->writeLog(NcclLogLevel::ERROR,"sentHash msg size != sender_node %d receiver_node %d message_size %lu flow_id ",sender_node,receiver_node,message_size);
      }
    }else{
      NcclLog->writeLog(NcclLogLevel::ERROR,"sentHash cann't find sender_node %d receiver_node %d message_size %lu",sender_node,receiver_node,message_size);
    }       
    #ifdef NS3_MTP
    cs.ExitSection();
    #endif
  }
sender_end_1st_section:
  return;
}


void notify_sender_packet_arrivered_receiver(int sender_node, int receiver_node,
                                    uint64_t message_size, AstraSim::ncclFlowTag flowTag) {
  int tag = flowTag.channel_id;
  if (sentHash.find(make_pair(tag, make_pair(sender_node, receiver_node))) !=
      sentHash.end()) {
    task1 t2 = sentHash[make_pair(tag, make_pair(sender_node, receiver_node))];
    AstraSim::SendPacketEventHandlerData* ehd = (AstraSim::SendPacketEventHandlerData*) t2.fun_arg;
    ehd->flowTag=flowTag;
    if (t2.count == message_size) {
      sentHash.erase(make_pair(tag, make_pair(sender_node, receiver_node)));
      if (nodeHash.find(make_pair(sender_node, 0)) == nodeHash.end()) {
        nodeHash[make_pair(sender_node, 0)] = message_size;
      } else {
        nodeHash[make_pair(sender_node, 0)] += message_size;
      }
      t2.msg_handler(t2.fun_arg);
    }
  }
}

void Figure2TxProgress(Ptr<RdmaQueuePair> qp, uint64_t sent) {
  Figure2WireKey wire(qp->sip.Get(), qp->dip.Get(), qp->sport,
                       qp->dport, qp->m_pg);
  const auto found = figure2WireQps.find(wire);
  if (found == figure2WireQps.end()) return;
  Figure2Qp &state = figure2Qps.at(found->second);
  // Pop before calling the application; its callback can post more work.
  while (!state.txPending.empty() && state.txPending.front().endSeq <= sent) {
    const Figure2Fragment fragment = state.txPending.front();
    state.txPending.pop_front();
    const int src = qp->m_src, dst = qp->m_dest;
    const auto key = std::make_pair(fragment.flowTag.current_flow_id,
                                    std::make_pair(src, dst));
    sent_chunksize[key] += fragment.bytes;
    if (is_sending_finished(src, dst, fragment.flowTag)) {
      const uint64_t total = sent_chunksize[key];
      sent_chunksize.erase(key);
      notify_sender_sending_finished(src, dst, total, fragment.flowTag);
    }
  }
}

void Figure2AckProgress(Ptr<RdmaQueuePair> qp, uint64_t received) {
  const auto found = figure2WireQps.find(
      Figure2WireKey(qp->sip.Get(), qp->dip.Get(), qp->sport,
                     qp->dport, qp->m_pg));
  if (found == figure2WireQps.end()) return;
  Figure2Qp &state = figure2Qps.at(found->second);
  const int src = std::get<0>(found->second);
  const int dst = std::get<1>(found->second);
  while (!state.rxPending.empty() && state.rxPending.front().endSeq <= received) {
    const Figure2Fragment fragment = state.rxPending.front();
    state.rxPending.pop_front();
    const auto key = std::make_pair(fragment.flowTag.current_flow_id,
                                    std::make_pair(src, dst));
    received_chunksize[key] += fragment.bytes;
    if (is_receive_finished(src, dst, fragment.flowTag)) {
      const uint64_t total = received_chunksize[key];
      received_chunksize.erase(key);
      notify_receiver_receive_data(src, dst, total, fragment.flowTag);
    }
  }
}

void qp_finish(FILE *fout, Ptr<RdmaQueuePair> q) {
  if (figure2WireQps.count(Figure2WireKey(q->sip.Get(), q->dip.Get(),
                                         q->sport, q->dport, q->m_pg)) != 0) {
    NS_ABORT_MSG("Figure 2 persistent QP unexpectedly terminated: sport="
                 << q->sport << " qp_error=" << q->m_qpError);
  }
  uint32_t sid = ip_to_node_id(q->sip), did = ip_to_node_id(q->dip);
  uint64_t base_rtt = pairRtt[sid][did], b = pairBw[sid][did];
  uint32_t total_bytes =
      q->m_size +
      ((q->m_size - 1) / packet_payload_size + 1) *
          (CustomHeader::GetStaticWholeHeaderSize() -
           IntHeader::GetStaticSize()); 
  uint64_t standalone_fct = base_rtt + total_bytes * 8000000000lu / b;
  fprintf(fout, "%08x %08x %u %u %lu %lu %lu %lu\n", q->sip.Get(), q->dip.Get(),
          q->sport, q->dport, q->m_size, q->startTime.GetTimeStep(),
          (Simulator::Now() - q->startTime).GetTimeStep(), standalone_fct);
  fflush(fout);

  AstraSim::ncclFlowTag flowTag;
  uint64_t notify_size;
  {
    #ifdef NS3_MTP
    MtpInterface::explicitCriticalSection cs;
    #endif
    Ptr<Node> dstNode = n.Get(did);
    Ptr<RdmaDriver> rdma = dstNode->GetObject<RdmaDriver>();
    rdma->m_rdma->DeleteRxQp(q->sip.Get(), q->m_pg, q->sport);
    MockNcclLog* NcclLog = MockNcclLog::getInstance();
    NcclLog->writeLog(NcclLogLevel::DEBUG,"qp finish, src:  %d did:  %d port:  %d total bytes:  %llu at the tick:  %d",sid,did,q->sport,q->m_size,AstraSim::Sys::boostedTick());
    if (sender_src_port_map.find(make_pair(q->sport, make_pair(sid, did))) ==
        sender_src_port_map.end()) {
      NcclLog->writeLog(NcclLogLevel::ERROR,"could not find the tag, there must be something wrong");
      exit(-1);
    }
    flowTag = sender_src_port_map[make_pair(q->sport, make_pair(sid, did))];
    sender_src_port_map.erase(make_pair(q->sport, make_pair(sid, did)));
    received_chunksize[std::make_pair(flowTag.current_flow_id,std::make_pair(sid,did))]+=q->m_size;
    if(!is_receive_finished(sid,did,flowTag)) {
      #ifdef NS3_MTP
      cs.ExitSection();
      #endif
      return; 
    }
    notify_size = received_chunksize[std::make_pair(flowTag.current_flow_id,std::make_pair(sid,did))];
    received_chunksize.erase(std::make_pair(flowTag.current_flow_id,std::make_pair(sid,did)));    
    #ifdef NS3_MTP
    cs.ExitSection();
    #endif
  }
  notify_receiver_receive_data(sid, did, notify_size, flowTag);
}

void send_finish(FILE *fout, Ptr<RdmaQueuePair> q) {
  if (figure2WireQps.count(Figure2WireKey(q->sip.Get(), q->dip.Get(),
                                         q->sport, q->dport, q->m_pg)) != 0) {
    return; // per-fragment TX progress handles Figure 2 completion
  }
  uint32_t sid = ip_to_node_id(q->sip), did = ip_to_node_id(q->dip);
  AstraSim::ncclFlowTag flowTag;
  MockNcclLog* NcclLog = MockNcclLog::getInstance();
  NcclLog->writeLog(NcclLogLevel::DEBUG,"[Packet sent from NIC] send finish, src:  %d did:  %d port:  %d srcip  %d dstip  %d total bytes:  %llu at the tick:  %d",sid,did,q->sport,q->sip,q->dip,q->m_size,AstraSim::Sys::boostedTick());
  uint64_t all_sent_chunksize;
  {
    #ifdef NS3_MTP
    MtpInterface::explicitCriticalSection cs;
    #endif
    flowTag = sender_src_port_map[make_pair(q->sport, make_pair(sid, did))];
    sent_chunksize[std::make_pair(flowTag.current_flow_id,std::make_pair(sid,did))]+=q->m_size;
    if(!is_sending_finished(sid,did,flowTag)) {
      #ifdef NS3_MTP
      cs.ExitSection();
      #endif
      return;
    }
    all_sent_chunksize = sent_chunksize[std::make_pair(flowTag.current_flow_id,std::make_pair(sid,did))];
    sent_chunksize.erase(std::make_pair(flowTag.current_flow_id,std::make_pair(sid,did)));
    #ifdef NS3_MTP
    cs.ExitSection();
    #endif
  }
  notify_sender_sending_finished(sid, did, all_sent_chunksize, flowTag);
}

int main1(string network_topo,string network_conf) {
  clock_t begint, endt;
  begint = clock();

  if (!ReadConf(network_topo,network_conf))
    return -1;
  SetConfig();
  SetupNetwork(qp_finish,send_finish);

  // IRN_PORT_V1: model-0 extension; legacy defaults when unset or zero.
  if (const char *irn = std::getenv("SIMAI_IRN_ENABLE")) {
    NS_ABORT_MSG_IF(std::string(irn) != "0" && std::string(irn) != "1",
                    "SIMAI_IRN_ENABLE must be 0 or 1");
    if (std::string(irn) == "1") {
      NS_ABORT_MSG_IF(rdma_transport_mode != 0 || cc_mode != 1 || rc_ack_retry_enable != 0,
                      "IRN v1 requires mode=0, DCQCN=1 and legacy RC retry disabled");
      auto number = [](const char *key, uint64_t fallback) -> uint64_t {
        const char *v = std::getenv(key);
        if (v == nullptr) return fallback;
        const std::string value(v);
        NS_ABORT_MSG_IF(value.empty() || value.find_first_not_of("0123456789") != std::string::npos,
                        "Invalid IRN integer: " << key);
        return std::stoull(value);
      };
      const uint64_t bdp = number("SIMAI_IRN_BDP_BYTES", maxBdp);
      // Derive timeouts from this topology, not 454/1350 us from the source
      // simulator's short-distance fabric. These are explicit v1 choices.
      const uint64_t low = number("SIMAI_IRN_RTO_LOW_NS", 2ULL * maxRtt);
      const uint64_t high = number("SIMAI_IRN_RTO_HIGH_NS", 4ULL * maxRtt);
      for (uint32_t i = 0; i < n.GetN(); ++i) {
        Ptr<RdmaDriver> driver = n.Get(i)->GetObject<RdmaDriver>();
        if (driver != nullptr) {
          driver->m_rdma->ConfigureIrn(bdp, low, high);
        }
      }
      std::cout << "IRN_CONFIG enabled=1 source=conweave_236a801"
                << " state=byte_sack_ranges bdp_bytes=" << bdp
                << " rto_low_ns=" << low << " rto_high_ns=" << high
                << " cc=dcqcn legacy_rc_retry=0" << std::endl;
    }
  }


  if (GetFigure2QpsPerChannel() != 0) {
    for (uint32_t i = 0; i < n.GetN(); ++i) {
      Ptr<RdmaDriver> driver = n.Get(i)->GetObject<RdmaDriver>();
      if (driver != nullptr) {
        driver->m_rdma->SetFlowProgressCallbacks(
            MakeCallback(&Figure2TxProgress),
            MakeCallback(&Figure2AckProgress));
      }
    }
  }

std::cout << "Running Simulation.\n";
  fflush(stdout);
  NS_LOG_INFO("Run Simulation.");

  endt = clock();
  return 0;
}
#endif