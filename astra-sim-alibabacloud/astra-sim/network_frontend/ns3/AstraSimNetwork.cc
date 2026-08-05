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

#include "astra-sim/system/AstraNetworkAPI.hh"
#include "astra-sim/system/Sys.hh"
#include "astra-sim/system/RecvPacketEventHadndlerData.hh"
#include "astra-sim/system/Common.hh"
#include "astra-sim/system/MockNcclLog.h"
#include "ns3/applications-module.h"
#include "ns3/core-module.h"
#include "ns3/csma-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "entry.h"
#include <execinfo.h>
#include <fstream>
#include <iostream>
#include <limits>
#include <queue>
#include <stdio.h>
#include <sstream>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>
#include <set>
#ifdef NS3_MTP
#include "ns3/mtp-interface.h"
#endif
#ifdef NS3_MPI
#include "ns3/mpi-interface.h"
#include <mpi.h>
#endif

#define RESULT_PATH "./ncclFlowModel_"

using namespace std;
using namespace ns3;

extern std::map<std::pair<std::pair<int, int>,int>, AstraSim::ncclFlowTag> receiver_pending_queue;
extern uint32_t node_num, switch_num, link_num, trace_num, nvswitch_num, gpus_per_server, ocs_num;
extern GPUType gpu_type;
extern std::vector<int>NVswitchs;

struct sim_event {
  void *buffer;
  uint64_t count;
  int type;
  int dst;
  int tag;
  string fnType;
};

class ASTRASimNetwork : public AstraSim::AstraNetworkAPI {
private:
  int npu_offset;

public:
  queue<sim_event> sim_event_queue;
  ASTRASimNetwork(int rank, int npu_offset) : AstraNetworkAPI(rank) {
    this->npu_offset = npu_offset;
  }
  ~ASTRASimNetwork() {}
  int sim_comm_size(AstraSim::sim_comm comm, int *size) { return 0; }
  int sim_finish() {
    for (auto it = nodeHash.begin(); it != nodeHash.end(); it++) {
      pair<int, int> p = it->first;
      if (p.second == 0) {
        std::cout << "sim_finish on sent, " << " Thread id: " << pthread_self() << std::endl;
        cout << "All data sent from node " << p.first << " is " << it->second
             << "\n";
      } else {
        std::cout << "sim_finish on received, " << " Thread id: " << pthread_self() << std::endl;
        cout << "All data received by node " << p.first << " is " << it->second
             << "\n";
      }
    }
    exit(0);
    return 0;
  }
  double sim_time_resolution() { return 0; }
  int sim_init(AstraSim::AstraMemoryAPI *MEM) { return 0; }
  AstraSim::timespec_t sim_get_time() {
    AstraSim::timespec_t timeSpec;
    timeSpec.time_val = Simulator::Now().GetNanoSeconds();
    return timeSpec;
  }
  virtual void sim_schedule(AstraSim::timespec_t delta,
                            void (*fun_ptr)(void *fun_arg), void *fun_arg) {
    task1 t;
    t.type = 2;
    t.fun_arg = fun_arg;
    t.msg_handler = fun_ptr;
    t.schTime = delta.time_val;
    Simulator::Schedule(NanoSeconds(t.schTime), t.msg_handler, t.fun_arg);
    return;
  }
  virtual int sim_send(void *buffer,   
                       uint64_t count, 
                       int type,       
                       int dst,
                       int tag,                       
                       AstraSim::sim_request *request, 
                       void (*msg_handler)(void *fun_arg), void *fun_arg) {
    dst += npu_offset;
    task1 t;
    t.src = rank;
    t.dest = dst;
    t.count = count;
    t.type = 0;
    t.fun_arg = fun_arg;
    t.msg_handler = msg_handler;
    {
      #ifdef NS3_MTP
      MtpInterface::explicitCriticalSection cs;
      #endif
      sentHash[make_pair(tag, make_pair(t.src, t.dest))] = t;
      #ifdef NS3_MTP
      cs.ExitSection();
      #endif
    }
    SendFlow(rank, dst, count, msg_handler, fun_arg, tag, request);
    return 0;
  }
  virtual int sim_recv(void *buffer, uint64_t count, int type, int src, int tag,
                       AstraSim::sim_request *request,
                       void (*msg_handler)(void *fun_arg), void *fun_arg) {
    #ifdef NS3_MTP
    MtpInterface::explicitCriticalSection cs;
    #endif
    MockNcclLog* NcclLog = MockNcclLog::getInstance();
    AstraSim::ncclFlowTag flowTag = request->flowTag;
    src += npu_offset;
    task1 t;
    t.src = src;
    t.dest = rank;
    t.count = count;
    t.type = 1;
    t.fun_arg = fun_arg;
    t.msg_handler = msg_handler;
    AstraSim::RecvPacketEventHadndlerData* ehd = (AstraSim::RecvPacketEventHadndlerData*) t.fun_arg;
    AstraSim::EventType event = ehd->event;
    tag = ehd->flowTag.tag_id;
    NcclLog->writeLog(NcclLogLevel::DEBUG,"[Receive event registration] src %d sim_recv on rank %d tag_id %d channdl id %d",src,rank,tag,ehd->flowTag.channel_id);
    
    if (recvHash.find(make_pair(tag, make_pair(t.src, t.dest))) !=
        recvHash.end()) {
      uint64_t count = recvHash[make_pair(tag, make_pair(t.src, t.dest))];
      if (count == t.count) {
        recvHash.erase(make_pair(tag, make_pair(t.src, t.dest)));
        assert(ehd->flowTag.child_flow_id == -1 && ehd->flowTag.current_flow_id == -1);
        if(receiver_pending_queue.count(std::make_pair(std::make_pair(rank, src),tag))!= 0) {
          AstraSim::ncclFlowTag pending_tag = receiver_pending_queue[std::make_pair(std::make_pair(rank, src),tag)];
          receiver_pending_queue.erase(std::make_pair(std::make_pair(rank,src),tag));
          ehd->flowTag = pending_tag;
        } 
        #ifdef NS3_MTP
        cs.ExitSection();
        #endif
        t.msg_handler(t.fun_arg);
        goto sim_recv_end_section;
      } else if (count > t.count) {
        recvHash[make_pair(tag, make_pair(t.src, t.dest))] = count - t.count;
        assert(ehd->flowTag.child_flow_id == -1 && ehd->flowTag.current_flow_id == -1);
        if(receiver_pending_queue.count(std::make_pair(std::make_pair(rank, src),tag))!= 0) {
          AstraSim::ncclFlowTag pending_tag = receiver_pending_queue[std::make_pair(std::make_pair(rank, src),tag)];
          receiver_pending_queue.erase(std::make_pair(std::make_pair(rank,src),tag));
          ehd->flowTag = pending_tag;
        } 
        #ifdef NS3_MTP
        cs.ExitSection();
        #endif
        t.msg_handler(t.fun_arg);
        goto sim_recv_end_section;
      } else {
        recvHash.erase(make_pair(tag, make_pair(t.src, t.dest)));
        t.count -= count;
        expeRecvHash[make_pair(tag, make_pair(t.src, t.dest))] = t;
      }
    } else {
      if (expeRecvHash.find(make_pair(tag, make_pair(t.src, t.dest))) ==
          expeRecvHash.end()) {
        expeRecvHash[make_pair(tag, make_pair(t.src, t.dest))] = t;
          NcclLog->writeLog(NcclLogLevel::DEBUG," [Packet arrived late, registering first] recvHash do not find expeRecvHash.new make src  %d dest  %d t.count:  %llu channel_id  %d current_flow_id  %d",t.src,t.dest,t.count,tag,flowTag.current_flow_id);
          
      } else {
        uint64_t expecount =
            expeRecvHash[make_pair(tag, make_pair(t.src, t.dest))].count;
          NcclLog->writeLog(NcclLogLevel::DEBUG," [Packet arrived late, re-registering] recvHash do not find expeRecvHash.add make src  %d dest  %d expecount:  %d t.count:  %d tag_id  %d current_flow_id  %d",t.src,t.dest,expecount,t.count,tag,flowTag.current_flow_id);
          
      }
    }
    #ifdef NS3_MTP
    cs.ExitSection();
    #endif

sim_recv_end_section:    
    return 0;
  }
  void handleEvent(int dst, int cnt) {
  }
};


namespace {

struct StaticFlowInput {
  uint32_t src;
  uint32_t dst;
  uint32_t pg;
  uint16_t dport;
  uint64_t bytes;
  double start_time;
  uint32_t index;
};

uint32_t static_flow_expected = 0;
uint32_t static_flow_completed = 0;
typedef std::pair<uint16_t, std::pair<uint32_t, uint32_t>> StaticFlowKey;
std::map<StaticFlowKey, uint32_t> static_flow_indices;
std::set<StaticFlowKey> static_flow_sent_logged;

void StaticFlowNoop(void *) {}

bool LoadStaticFlows(const std::string &path,
                     std::vector<StaticFlowInput> *flows) {
  std::ifstream input(path.c_str());
  if (!input.is_open()) {
    std::cerr << "Error: cannot open static flow file: " << path << std::endl;
    return false;
  }

  uint32_t flow_count = 0;
  if (!(input >> flow_count)) {
    std::cerr << "Error: cannot read flow count from: " << path << std::endl;
    return false;
  }

  flows->clear();
  flows->reserve(flow_count);
  for (uint32_t i = 0; i < flow_count; ++i) {
    StaticFlowInput flow;
    uint32_t dport = 0;
    if (!(input >> flow.src >> flow.dst >> flow.pg >> dport >> flow.bytes >>
          flow.start_time)) {
      std::cerr << "Error: invalid static flow entry " << i
                << ". Expected: src dst pg dport bytes start_time" << std::endl;
      return false;
    }

    if (dport > std::numeric_limits<uint16_t>::max()) {
      std::cerr << "Error: destination port out of range in flow " << i
                << std::endl;
      return false;
    }
    if (flow.bytes == 0) {
      std::cerr << "Error: flow size must be greater than zero in flow " << i
                << std::endl;
      return false;
    }
    if (flow.start_time < 0.0) {
      std::cerr << "Error: flow start time must not be negative in flow " << i
                << std::endl;
      return false;
    }

    flow.dport = static_cast<uint16_t>(dport);
    flow.index = i;
    flows->push_back(flow);
  }

  std::string trailing;
  if (input >> trailing) {
    std::cerr << "Error: static flow file contains data after " << flow_count
              << " entries" << std::endl;
    return false;
  }
  return true;
}

void StaticQpFinish(FILE *fout, Ptr<RdmaQueuePair> q) {
  const uint32_t sid = ip_to_node_id(q->sip);
  const uint32_t did = ip_to_node_id(q->dip);
  const uint64_t base_rtt = pairRtt[sid][did];
  const uint64_t bandwidth = pairBw[sid][did];
  const uint64_t packet_count =
      (q->m_size + packet_payload_size - 1) / packet_payload_size;
  const uint64_t total_bytes =
      q->m_size + packet_count *
                      (CustomHeader::GetStaticWholeHeaderSize() -
                       IntHeader::GetStaticSize());
  const uint64_t standalone_fct =
      bandwidth == 0 ? 0 : base_rtt + total_bytes * 8000000000ULL / bandwidth;

  if (fout != nullptr) {
    fprintf(fout, "%08x %08x %u %u %lu %lu %lu %lu\n", q->sip.Get(),
            q->dip.Get(), q->sport, q->dport, q->m_size,
            q->startTime.GetTimeStep(),
            (Simulator::Now() - q->startTime).GetTimeStep(), standalone_fct);
    fflush(fout);
  }

  Ptr<Node> dst_node = n.Get(did);
  Ptr<RdmaDriver> rdma = dst_node->GetObject<RdmaDriver>();
  if (rdma != nullptr && rdma->m_rdma != nullptr) {
    rdma->m_rdma->DeleteRxQp(q->sip.Get(), q->m_pg, q->sport);
  }

  uint32_t flow_index = std::numeric_limits<uint32_t>::max();
  StaticFlowKey key =
      std::make_pair(q->sport, std::make_pair(sid, did));
  std::map<StaticFlowKey, uint32_t>::iterator it =
      static_flow_indices.find(key);
  if (it != static_flow_indices.end()) {
    flow_index = it->second;
    static_flow_indices.erase(it);
  }

  ++static_flow_completed;
  std::cout << "[STATIC FLOW COMPLETE]"
            << " index=" << flow_index
            << " t_ns=" << Simulator::Now().GetNanoSeconds()
            << " src=" << sid
            << " dst=" << did
            << " sport=" << q->sport
            << " dport=" << q->dport
            << " bytes=" << q->m_size
            << " completed=" << static_flow_completed
            << "/" << static_flow_expected << std::endl;
}

void StaticSendFinish(FILE *, Ptr<RdmaQueuePair> q) {
  // In Mode 2, send-complete may be raised whenever the currently posted
  // portion has drained. Report flow-level send completion only after all
  // bytes have been posted.
  if (q == nullptr || q->GetUnpostedBytes() != 0) {
    return;
  }

  const uint32_t sid = ip_to_node_id(q->sip);
  const uint32_t did = ip_to_node_id(q->dip);

  const StaticFlowKey key =
      std::make_pair(q->sport, std::make_pair(sid, did));

  // Report each flow-level send completion only once.
  if (!static_flow_sent_logged.insert(key).second) {
    return;
  }

  uint32_t flow_index = std::numeric_limits<uint32_t>::max();

  std::map<StaticFlowKey, uint32_t>::const_iterator it =
      static_flow_indices.find(key);
  if (it != static_flow_indices.end()) {
    flow_index = it->second;
  }

  std::cout << "[STATIC FLOW SENT]"
            << " index=" << flow_index
            << " t_ns=" << Simulator::Now().GetNanoSeconds()
            << " src=" << sid
            << " dst=" << did
            << " sport=" << q->sport
            << " bytes=" << q->m_size
            << std::endl;
}

bool InstallStaticFlows(const std::vector<StaticFlowInput> &flows) {
  static_flow_expected = static_cast<uint32_t>(flows.size());
  static_flow_completed = 0;
  static_flow_indices.clear();
  static_flow_sent_logged.clear();

  for (const StaticFlowInput &flow : flows) {
    if (flow.src >= n.GetN() || flow.dst >= n.GetN()) {
      std::cerr << "Error: static flow " << flow.index
                << " has node id outside the topology" << std::endl;
      return false;
    }
    if (n.Get(flow.src)->GetNodeType() != 0 ||
        n.Get(flow.dst)->GetNodeType() != 0) {
      std::cerr << "Error: static flow " << flow.index
                << " endpoints must both be GPU/host nodes" << std::endl;
      return false;
    }

    const uint16_t sport = portNumber[flow.src][flow.dst]++;
    const uint32_t window =
        has_win
            ? static_cast<uint32_t>(
                  global_t == 1 ? maxBdp : pairBdp[n.Get(flow.src)][n.Get(flow.dst)])
            : 0;
    const uint64_t base_rtt =
        global_t == 1 ? maxRtt : pairRtt[flow.src][flow.dst];

    static_flow_indices[
        std::make_pair(sport, std::make_pair(flow.src, flow.dst))] =
        flow.index;

    RdmaClientHelper client_helper(
        flow.pg, serverAddress[flow.src], serverAddress[flow.dst], sport,
        flow.dport, flow.bytes, window, base_rtt, StaticFlowNoop, nullptr,
        flow.index, flow.src, flow.dst);
    ApplicationContainer applications = client_helper.Install(n.Get(flow.src));
    applications.Start(Seconds(flow.start_time));

    std::cout << "[STATIC FLOW INSTALLED]"
              << " index=" << flow.index
              << " src=" << flow.src
              << " dst=" << flow.dst
              << " sport=" << sport
              << " dport=" << flow.dport
              << " pg=" << flow.pg
              << " bytes=" << flow.bytes
              << " start_s=" << flow.start_time << std::endl;
  }
  return true;
}

int InitializeStaticNetwork(const std::string &network_topo,
                            const std::string &network_conf) {
  if (!ReadConf(network_topo, network_conf)) {
    return -1;
  }
  SetConfig();
  SetupNetwork(StaticQpFinish, StaticSendFinish);
  std::cout << "Running static-flow simulation.\n";
  fflush(stdout);
  return 0;
}

} // namespace

struct user_param {
  int thread;
  string workload;
  string flow_file;
  string network_topo;
  string network_conf;
  double static_stop_time;
  user_param() {
    thread = 1;
    workload = "";
    flow_file = "";
    network_topo = "";
    network_conf = "";
    static_stop_time = 0.4;
  };
  ~user_param(){};
};

static int user_param_prase(int argc,char * argv[],struct user_param* user_param){
  int opt;
  while ((opt = getopt(argc,argv,"ht:w:f:n:c:x:"))!=-1){
    switch (opt)
    {
    case 'h':
      std::cout<<"-t    number of threads, default 1"<<std::endl;
      std::cout<<"-w    ASTRA workload file"<<std::endl;
      std::cout<<"-f    static RDMA flow file"<<std::endl;
      std::cout<<"-n    network topology"<<std::endl;
      std::cout<<"-c    network configuration"<<std::endl;
      std::cout<<"-x    static-flow stop time in seconds, default 0.4"<<std::endl;
      return 1;
    case 't':
      user_param->thread = stoi(optarg);
      break;
    case 'w':
      user_param->workload = optarg;
      break;
    case 'f':
      user_param->flow_file = optarg;
      break;
    case 'n':
      user_param->network_topo = optarg;
      break;
    case 'c':
      user_param->network_conf = optarg;
      break;
    case 'x':
      user_param->static_stop_time = stod(optarg);
      break;
    default:
      std::cerr<<"-h    help message"<<std::endl;
      return 1;
    }
  }

  if (user_param->network_topo.empty() || user_param->network_conf.empty()) {
    std::cerr << "Error: -n and -c are required" << std::endl;
    return 1;
  }
  if (user_param->workload.empty() == user_param->flow_file.empty()) {
    std::cerr << "Error: specify exactly one of -w or -f" << std::endl;
    return 1;
  }
  if (!user_param->flow_file.empty() && user_param->static_stop_time <= 0.0) {
    std::cerr << "Error: static-flow stop time must be greater than zero"
              << std::endl;
    return 1;
  }
  return 0 ;
}

int main(int argc, char *argv[]) {
  struct user_param user_param;
  MockNcclLog::set_log_name("SimAI.log");
  MockNcclLog* NcclLog = MockNcclLog::getInstance();
  NcclLog->writeLog(NcclLogLevel::INFO," init SimAI.log ");
  if(user_param_prase(argc,argv,&user_param)){
    return 0;
  }
  #ifdef NS3_MTP
  MtpInterface::Enable(user_param.thread);
  #endif
  
  if (!user_param.flow_file.empty()) {
    std::vector<StaticFlowInput> static_flows;
    if (!LoadStaticFlows(user_param.flow_file, &static_flows)) {
      return 2;
    }
    if (InitializeStaticNetwork(user_param.network_topo,
                                user_param.network_conf) != 0) {
      return 2;
    }
    if (!InstallStaticFlows(static_flows)) {
      Simulator::Destroy();
      return 2;
    }

    Simulator::Stop(Seconds(user_param.static_stop_time));
    Simulator::Run();
    DumpOcsStatsFinal();
    std::cout << "[STATIC FLOW SUMMARY] completed=" << static_flow_completed
              << " expected=" << static_flow_expected
              << " stop_time_s=" << user_param.static_stop_time << std::endl;
    Simulator::Destroy();
#ifdef NS3_MPI
    MpiInterface::Disable();
#endif
    return static_flow_completed == static_flow_expected ? 0 : 3;
  }

  main1(user_param.network_topo,user_param.network_conf);
  int nodes_num = node_num - switch_num - ocs_num;
  int gpu_num = node_num - nvswitch_num - switch_num - ocs_num;

  std::map<int, int> node2nvswitch; 
  for(int i = 0; i < gpu_num; ++ i) {
    node2nvswitch[i] = gpu_num + i / gpus_per_server;
  }
  for(int i = gpu_num; i < gpu_num + nvswitch_num; ++ i){
    node2nvswitch[i] = i;
    NVswitchs.push_back(i);
  } 

  LogComponentEnable("OnOffApplication", LOG_LEVEL_INFO);
  LogComponentEnable("PacketSink", LOG_LEVEL_INFO);
  LogComponentEnable("GENERIC_SIMULATION", LOG_LEVEL_INFO);

  std::vector<ASTRASimNetwork *> networks(nodes_num, nullptr);
  std::vector<AstraSim::Sys *> systems(nodes_num, nullptr);

  for (int j = 0; j < nodes_num; j++) {
    networks[j] =
        new ASTRASimNetwork(j ,0);
    systems[j ] = new AstraSim::Sys(
        networks[j], 
        nullptr,                  
        j,                        
        0,               
        1,                        
        {nodes_num},        
        {1},          
        "", 
        user_param.workload, 
        1, 
        1,          
        1,          
        1,
        0,                 
        RESULT_PATH, 
        "test1",            
        true,               
        false,               
        gpu_type,
        {gpu_num},
        NVswitchs,
        gpus_per_server
    );
    systems[j ]->nvswitch_id = node2nvswitch[j];
    systems[j ]->num_gpus = nodes_num - nvswitch_num;
  }
  for (int i = 0; i < nodes_num; i++) {
    systems[i]->workload->fire();
  }
  std::cout << "simulator run " << std::endl;

  Simulator::Run();
  DumpOcsStatsFinal();
  Simulator::Stop(Seconds(2000000000));
  Simulator::Destroy();
  
  #ifdef NS3_MPI
  MpiInterface::Disable ();
  #endif
  return 0;
}
