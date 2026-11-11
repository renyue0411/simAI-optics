// SPDX-License-Identifier: GPL-2.0-only
// Explicit experiment initialization; never used as a probe measurement oracle.
#ifndef SIMAI_DCI_PM_DELAY_EXPERIMENT_H
#define SIMAI_DCI_PM_DELAY_EXPERIMENT_H
#include "dci-wan-profile.h"
#include <algorithm>
#include <cstdlib>
#include <limits>
#include <set>
#include <string>
#include <vector>

namespace DciPmDelayExperiment {
inline const std::string& Profile() {
  static const std::string profile = []() {
    const char* raw = std::getenv("SIMAI_PM_DELAY_PROFILE");
    std::string value = raw ? raw : "none";
    NS_ABORT_MSG_IF(value != "none" && value != "equal" && value != "asymmetric" &&
                    value != "four-delay" && value != "topology",
                    "Unknown PM initialization mode; use topology for runtime WAN parameters");
    return value;
  }();
  return profile;
}
inline bool Enabled() { return Profile() != "none"; }
inline bool AllowParallel(uint32_t a, uint32_t b) {
  return Enabled() && ((a == 6 && b == 7) || (a == 7 && b == 6));
}
inline void ApplyBounds(NodeContainer& nodes) {
  if (!Enabled()) return;
  NS_ABORT_MSG_IF(DciHeterogeneous::Enabled(), "Do not combine the old four-WAN profile and PM delay experiment");
  NS_ABORT_MSG_IF(nodes.GetN() != 8, "PM delay experiment requires the audited four-GPU/two-gateway topology");
  size_t pathCount = 0;
  for (uint32_t id : {6U, 7U}) {
    auto gateway = nodes.Get(id);
    auto peer = nodes.Get(id == 6 ? 7 : 6);
    const auto& wan = neighborLinks.at(gateway).at(peer);
    NS_ABORT_MSG_IF(wan.size() < 2 || (pathCount && wan.size() != pathCount),
                    "PM benchmark requires matching, nonempty multipath WAN adjacencies");
    pathCount = wan.size();
    std::set<uint32_t> ports;
    for (const auto& link : wan) {
      NS_ABORT_MSG_IF(!link.up || !link.bw || link.idx >= gateway->GetNDevices() ||
                      !ports.insert(link.idx).second, "Invalid runtime WAN adjacency");
      std::cout << "DCI_PM_TOPO_LINK node=" << id << " peer_node=" << peer->GetId()
        << " port=" << link.idx << " rate_bps=" << link.bw << " delay_ns=" << link.delay
        << " control_serial_ns=" << DataRate(link.bw).CalculateBytesTxTime(82).GetNanoSeconds()
        << " source=experiment_initialization\n";
    }
    auto sw = DynamicCast<SwitchNode>(gateway);
    NS_ABORT_MSG_IF(!sw, "PM gateway is not a SwitchNode");
    DciWanProfile::Apply(sw, ports); // DCI_WAN_PROFILE_V1
    uint64_t checkedHeadroom = 0;
    for (uint32_t port = 1; port < gateway->GetNDevices(); ++port)
      checkedHeadroom += sw->m_mmu->headroom[port];
    NS_ABORT_MSG_IF(checkedHeadroom > std::numeric_limits<uint32_t>::max() ||
                    checkedHeadroom != sw->m_mmu->total_hdrm,
                    "MMU headroom overflow or inconsistent initialization");
    const uint64_t reserved = uint64_t(sw->m_mmu->total_hdrm) + sw->m_mmu->total_rsrv;
    NS_ABORT_MSG_IF(reserved >= sw->m_mmu->buffer_size,
                    "PM gateway buffer does not cover configured headroom/reservation");
    std::cout << "DCI_PM_DELAY_MMU node=" << id << " buffer_bytes=" << sw->m_mmu->buffer_size
      << " reserved_bytes=" << reserved << " headroom_bytes=" << sw->m_mmu->total_hdrm
      << " port_reserved_bytes=" << sw->m_mmu->total_rsrv
      << " shared_capacity_bytes=" << (uint64_t(sw->m_mmu->buffer_size) - reserved) << '\n';
  }
  for (uint32_t src = 0; src < 4; ++src) {
    for (uint32_t dst = 0; dst < 4; ++dst) {
      if ((src < 2) == (dst < 2)) continue;
      auto a = nodes.Get(src < 2 ? 6 : 7);
      auto b = nodes.Get(dst < 2 ? 6 : 7);
      const auto& accessA = neighborLinks.at(nodes.Get(src)).at(a);
      const auto& accessB = neighborLinks.at(b).at(nodes.Get(dst));
      const auto& wan = neighborLinks.at(a).at(b);
      NS_ABORT_MSG_IF(accessA.size() != 1 || accessB.size() != 1 ||
        !accessA[0].up || !accessB[0].up || !accessA[0].bw || !accessB[0].bw,
        "PM benchmark requires one valid direct access link per GPU");
      uint64_t largestRtt = 0, largestBdp = 0;
      const uint64_t bitsNs = uint64_t(packet_payload_size) * 8 * 1000000000ULL;
      for (const auto& link : wan) {
        const uint64_t propagation = accessA[0].delay + link.delay + accessB[0].delay;
        const uint64_t serial = bitsNs / accessA[0].bw + bitsNs / link.bw + bitsNs / accessB[0].bw;
        const uint64_t rtt = propagation * 2 + serial;
        const uint64_t rate = std::min(link.bw, std::min(accessA[0].bw, accessB[0].bw));
        const uint64_t bdp = static_cast<uint64_t>(static_cast<long double>(rtt) * rate / 8000000000.0L);
        largestRtt = std::max(largestRtt, rtt);
        largestBdp = std::max(largestBdp, bdp);
        std::cout << "DCI_PM_DELAY_PATH src=" << src << " dst=" << dst
          << " wan_port=" << link.idx << " rate_bps=" << link.bw << " delay_ns=" << link.delay
          << " rtt_bound_ns=" << rtt << " bdp_bytes=" << bdp << '\n';
      }
      NS_ABORT_MSG_IF(largestBdp > std::numeric_limits<uint32_t>::max(), "PM experiment QP window exceeds supported size");
      pairRtt[src][dst] = largestRtt;
      pairBdp[nodes.Get(src)][nodes.Get(dst)] = largestBdp;
      maxRtt = std::max(maxRtt, largestRtt);
      maxBdp = std::max(maxBdp, largestBdp);
    }
  }
  std::cout << "DCI_PM_DELAY_CONFIG enabled=1 profile=" << Profile()
    << " path_count=" << pathCount << " attributes=runtime_topology"
    << " window_policy=max_candidate_bdp rtt_policy=max_candidate_rtt"
    << " max_rtt_ns=" << maxRtt << " max_bdp_bytes=" << maxBdp
    << " source=transport_initialization_only\n";
}
} // namespace DciPmDelayExperiment
#endif
