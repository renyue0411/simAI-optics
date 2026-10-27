// DCI_SPRAY_V1: experiment-only observation and a common simulation-time budget.
#ifndef SIMAI_DCI_SPRAY_EXPERIMENT_H
#define SIMAI_DCI_SPRAY_EXPERIMENT_H
#include "ns3/abort.h"
#include "ns3/node-container.h"
#include "ns3/rdma-driver.h"
#include "ns3/rdma-hw.h"
#include "ns3/simulator.h"
#include "ns3/switch-node.h"
#include <cerrno>
#include <cstdlib>
#include <iostream>
#include <string>

namespace DciSprayExperiment {
inline bool &DeadlineHit() { static bool hit = false; return hit; }
inline bool Enabled() { return std::getenv("SIMAI_DCI_EXPERIMENT_MS") != nullptr; }

inline void Dump(const ns3::NodeContainer &nodes, bool completed) {
  if (!Enabled()) return;
  ns3::SwitchNode::PrintDciRoutingStats();
  for (uint32_t i = 0; i < nodes.GetN(); ++i) {
    auto driver = nodes.Get(i)->GetObject<ns3::RdmaDriver>();
    if (driver == nullptr) continue;
    auto hw = driver->m_rdma;
    uint64_t retransBytes = 0, retransPackets = 0, timeouts = 0, nacks = 0;
    uint64_t outstanding = 0, acked = 0, posted = 0, received = 0, errors = 0;
    for (const auto &entry : hw->m_qpMap) {
      auto qp = entry.second;
      retransBytes += qp->m_retransBytes;
      retransPackets += qp->m_retransPackets;
      timeouts += qp->m_timeoutCount;
      nacks += qp->m_nackCount;
      outstanding += qp->m_highestSentSeq - qp->snd_una;
      acked += qp->snd_una;
      posted += qp->m_postedLimit;
      if (qp->m_qpError) ++errors;
    }
    for (const auto &entry : hw->m_rxQpMap)
      received += entry.second->ReceiverNextExpectedSeq;
    std::cout << "DCI_TRANSPORT_STATS node=" << nodes.Get(i)->GetId()
              << " irn=" << hw->m_irnEnabled << " live_qps=" << hw->m_qpMap.size()
              << " retrans_bytes=" << retransBytes << " retrans_packets=" << retransPackets
              << " timeouts=" << timeouts << " nack_count=" << nacks
              << " outstanding_bytes=" << outstanding << " acked_bytes=" << acked
              << " posted_bytes=" << posted << " received_contiguous_bytes=" << received
              << " qp_errors=" << errors << " completed=" << completed
              << " scope=live_persistent_qps" << std::endl;
    // sim_finish already prints these on success; deadline needs its own dump.
    if (!completed) hw->PrintIrnStats();
  }
  std::cout << "DCI_EXPERIMENT_END completed=" << completed
            << " time_ns=" << ns3::Simulator::Now().GetNanoSeconds()
            << " reason=" << (completed ? "collective_done" : "simulation_budget")
            << std::endl;
}

inline void OnDeadline(ns3::NodeContainer nodes) {
  DeadlineHit() = true;
  Dump(nodes, false);
  ns3::Simulator::Stop();
}

inline void Schedule(const ns3::NodeContainer &nodes) {
  if (!Enabled()) return;
  const char *value = std::getenv("SIMAI_DCI_EXPERIMENT_MS");
  char *end = nullptr;
  errno = 0;
  unsigned long long ms = std::strtoull(value, &end, 10);
  NS_ABORT_MSG_IF(value[0] < '0' || value[0] > '9' || errno != 0 || end == value ||
                  *end != '\0' || ms == 0 || ms > 10000,
                  "SIMAI_DCI_EXPERIMENT_MS must be 1..10000");
  std::cout << "DCI_EXPERIMENT_CONFIG budget_ms=" << ms
            << " observer_version=1" << std::endl;
  ns3::Simulator::Schedule(ns3::MilliSeconds(ms), &OnDeadline, nodes);
}
} // namespace DciSprayExperiment
#endif
