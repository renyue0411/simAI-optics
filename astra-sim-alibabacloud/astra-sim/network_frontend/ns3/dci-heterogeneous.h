// DCI_HETERO_V1: fixed four-WAN experiment; included after common.h globals.
#ifndef SIMAI_DCI_HETEROGENEOUS_H
#define SIMAI_DCI_HETEROGENEOUS_H
#include <algorithm>
#include <cstdlib>
#include <limits>
#include <string>

namespace DciHeterogeneous {
inline bool Enabled() {
  static bool enabled = []() {
    const char *value = std::getenv("SIMAI_DCI_HETEROGENEOUS");
    if (value == nullptr) return false;
    NS_ABORT_MSG_IF(std::string(value) != "0" && std::string(value) != "1",
                    "SIMAI_DCI_HETEROGENEOUS must be 0 or 1");
    return std::string(value) == "1";
  }();
  return enabled;
}
inline bool WanPair(uint32_t a, uint32_t b) {
  return (a == 6 && b == 7) || (a == 7 && b == 6);
}
inline void CheckMmu(Ptr<SwitchMmu> mmu, uint32_t id) {
  if (!Enabled()) return;
  const uint64_t reserved = static_cast<uint64_t>(mmu->total_hdrm) + mmu->total_rsrv;
  NS_ABORT_MSG_IF(reserved >= mmu->buffer_size,
                  "Switch buffer must exceed PFC headroom plus reserved bytes");
  std::cout << "DCI_HET_MMU node=" << id << " buffer_bytes=" << mmu->buffer_size
            << " headroom_bytes=" << mmu->total_hdrm
            << " reserved_bytes=" << mmu->total_rsrv << std::endl;
}
inline void ApplyBounds(NodeContainer &nodes) {
  if (!Enabled()) return;
  NS_ABORT_MSG_IF(nodes.GetN() != 8, "DCI heterogeneous benchmark requires 8 topology nodes");
  const uint64_t rates[4] = {400000000000ULL, 200000000000ULL,
                            100000000000ULL, 100000000000ULL};
  const uint64_t delays[4] = {1000000, 10000000, 1000000, 10000000};
  for (uint32_t src = 0; src < 4; ++src) {
    for (uint32_t dst = 0; dst < 4; ++dst) {
      if ((src < 2) == (dst < 2)) continue;
      Ptr<Node> a = nodes.Get(src < 2 ? 6 : 7);
      Ptr<Node> b = nodes.Get(dst < 2 ? 6 : 7);
      const auto &accessA = neighborLinks[nodes.Get(src)][a];
      const auto &accessB = neighborLinks[b][nodes.Get(dst)];
      const auto &wan = neighborLinks[a][b];
      NS_ABORT_MSG_IF(accessA.size() != 1 || accessB.size() != 1 || wan.size() != 4,
                      "Unexpected access/WAN structure");
      uint64_t worstRtt = 0, largestBdp = 0;
      for (size_t k = 0; k < wan.size(); ++k) {
        const auto &link = wan[k];
        NS_ABORT_MSG_IF(link.idx != k + 3 || link.bw != rates[k] || link.delay != delays[k],
                        "WAN order/rate/delay differs from the declared heterogeneous profile");
        const uint64_t delay = accessA[0].delay + link.delay + accessB[0].delay;
        // Match the original simulator formula: round-trip propagation plus
        // one forward DATA packet's serialization across the three links.
        const uint64_t bitsNs = static_cast<uint64_t>(packet_payload_size) * 8 * 1000000000ULL;
        const uint64_t serial = bitsNs / accessA[0].bw + bitsNs / link.bw + bitsNs / accessB[0].bw;
        const uint64_t rtt = delay * 2 + serial;
        const uint64_t bottleneck = std::min(link.bw, std::min(accessA[0].bw, accessB[0].bw));
        const uint64_t bdp = static_cast<uint64_t>(
            static_cast<long double>(rtt) * bottleneck / 8000000000.0L);
        worstRtt = std::max(worstRtt, rtt);
        largestBdp = std::max(largestBdp, bdp);
        std::cout << "DCI_HET_PATH src=" << src << " dst=" << dst << " wan_port=" << link.idx
                  << " rate_bps=" << link.bw << " delay_ns=" << link.delay
                  << " rtt_bound_ns=" << rtt << " bdp_bytes=" << bdp << std::endl;
      }
      NS_ABORT_MSG_IF(largestBdp > std::numeric_limits<uint32_t>::max(),
                      "QP window exceeds the existing 32-bit window field");
      // One conservative, policy-independent window bound for this pair.
      // This is not a measured RTT or a new path-management algorithm.
      pairRtt[src][dst] = worstRtt;
      pairBdp[nodes.Get(src)][nodes.Get(dst)] = largestBdp;
      maxRtt = std::max(maxRtt, worstRtt);
      maxBdp = std::max(maxBdp, largestBdp);
    }
  }
  std::cout << "DCI_HET_CONFIG enabled=1 profile=400-200-100-100g_1-10-1-10ms"
            << " window_policy=max_candidate_bdp rtt_policy=max_candidate_rtt"
            << " max_rtt_ns=" << maxRtt << " max_bdp_bytes=" << maxBdp << std::endl;
}
} // namespace DciHeterogeneous
#endif
