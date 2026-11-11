// SPDX-License-Identifier: GPL-2.0-only
// Deterministic receiver-side blackhole fixture; no path-manager oracle.
#ifndef SIMAI_DCI_PM_FAULT_EXPERIMENT_H
#define SIMAI_DCI_PM_FAULT_EXPERIMENT_H
#include "ns3/abort.h"
#include "ns3/custom-header.h"
#include "ns3/error-model.h"
#include "ns3/node-container.h"
#include "ns3/pointer.h"
#include "ns3/qbb-net-device.h"
#include "ns3/simulator.h"
#include <cstdlib>
#include <iostream>
#include <map>
#include <string>
#include <vector>

namespace DciPmFault {
inline uint32_t Option(const char* name, uint32_t fallback) {
  const char* raw = std::getenv(name);
  if (!raw) return fallback;
  std::string text(raw);
  NS_ABORT_MSG_IF(text.empty(), "Empty fault parameter");
  uint64_t value = 0;
  for (char c : text) {
    NS_ABORT_MSG_IF(c < '0' || c > '9', "Invalid fault parameter: " << name);
    value = value * 10 + c - '0';
    NS_ABORT_MSG_IF(value > 10000000, "Fault parameter exceeds limit");
  }
  return value;
}
inline bool IsData(ns3::Ptr<const ns3::Packet> packet, bool& probe) {
  ns3::CustomHeader header(ns3::CustomHeader::L2_Header | ns3::CustomHeader::L3_Header |
                           ns3::CustomHeader::L4_Header);
  header.getInt = 0;
  packet->PeekHeader(header);
  probe = header.IsDciPathControl();
  return header.l3Prot == 0x11 && !probe;
}
class Blackhole : public ns3::ErrorModel {
public:
  static ns3::TypeId GetTypeId() {
    static ns3::TypeId tid = ns3::TypeId("ns3::DciPmBlackhole")
      .SetParent<ns3::ErrorModel>().AddConstructor<Blackhole>();
    return tid;
  }
  ns3::Ptr<ns3::ErrorModel> original;
  bool blocked = false;
  uint64_t dataDrops = 0, dataBytes = 0, probeDrops = 0, otherDrops = 0;
private:
  bool DoCorrupt(ns3::Ptr<ns3::Packet> packet) override {
    // Preserve the pre-existing error model's calls and its random behavior.
    const bool baseDrop = original && original->IsCorrupt(packet);
    if (!blocked) return baseDrop;
    bool probe = false;
    if (IsData(packet, probe)) { ++dataDrops; dataBytes += packet->GetSize(); }
    else if (probe) ++probeDrops;
    else ++otherDrops;
    return true;
  }
  void DoReset() override { blocked = false; dataDrops = dataBytes = probeDrops = otherDrops = 0; }
};
struct Traffic {
  uint32_t node = 0, port = 0;
  uint64_t data[3] = {0,0,0}, probes[3] = {0,0,0};
};
struct Gate {
  uint32_t node, port;
  ns3::Ptr<ns3::QbbNetDevice> device;
  ns3::Ptr<Blackhole> error;
};
struct State {
  bool installed = false, enabled = false, dumped = false;
  uint32_t index = 0, startUs = 50000, endUs = 100000, events = 0;
  std::vector<Gate> gates;
  std::map<std::pair<uint32_t,uint32_t>, Traffic> traffic;
};
inline State& Get() { static State state; return state; }
inline void OnTx(Traffic* counter, ns3::Ptr<const ns3::Packet> packet) {
  const auto& state = Get();
  const auto now = ns3::Simulator::Now().GetMicroSeconds();
  const uint32_t window = now < state.startUs ? 0 : now < state.endUs ? 1 : 2;
  bool probe = false;
  if (IsData(packet, probe)) ++counter->data[window];
  else if (probe) ++counter->probes[window];
}
inline void Toggle(bool blocked) {
  auto& state = Get();
  for (auto& gate : state.gates) {
    NS_ABORT_MSG_IF(!gate.device->IsLinkUp(), "Fault fixture must keep the interface UP");
    gate.error->blocked = blocked;
  }
  ++state.events;
  std::cout << "DCI_PM_FAULT_EVENT action=" << (blocked ? "blackhole" : "restore")
    << " time_ns=" << ns3::Simulator::Now().GetNanoSeconds()
    << " link_stays_up=1 scope=both_receivers\n";
}
template <typename LinkMap>
inline void Install(const ns3::NodeContainer& nodes, const LinkMap& links) {
  auto& state = Get();
  NS_ABORT_MSG_IF(state.installed, "Fault fixture installed twice");
  state.installed = true;
  state.enabled = Option("SIMAI_PM_FAULT_ENABLE", 0) == 1;
  if (!state.enabled) return; // No objects, timers or trace callbacks in old cases.
  state.index = Option("SIMAI_PM_FAULT_PATH_INDEX", 0);
  state.startUs = Option("SIMAI_PM_FAULT_START_US", 50000);
  state.endUs = Option("SIMAI_PM_FAULT_END_US", 100000);
  NS_ABORT_MSG_IF(nodes.GetN() != 8 || !state.startUs || state.endUs <= state.startUs,
                  "Invalid fault interval or benchmark layout");
  std::vector<ns3::Ptr<ns3::Channel>> selectedChannels;
  for (uint32_t id : {6U,7U}) {
    auto node = nodes.Get(id), peer = nodes.Get(id == 6 ? 7 : 6);
    const auto& wan = links.at(node).at(peer);
    NS_ABORT_MSG_IF(state.index >= wan.size(), "Fault candidate index outside WAN adjacency");
    for (const auto& link : wan) {
      auto device = ns3::DynamicCast<ns3::QbbNetDevice>(node->GetDevice(link.idx));
      NS_ABORT_MSG_IF(!device || !device->IsLinkUp(), "Fault fixture requires live Qbb WAN devices");
      auto& counter = state.traffic[std::make_pair(id, link.idx)];
      counter.node = id; counter.port = link.idx;
      NS_ABORT_MSG_IF(!device->TraceConnectWithoutContext("PhyTxBegin",
        ns3::MakeBoundCallback(&OnTx, &counter)), "Missing actual WAN TX trace");
    }
    const uint32_t port = wan[state.index].idx;
    auto device = ns3::DynamicCast<ns3::QbbNetDevice>(node->GetDevice(port));
    ns3::PointerValue previous;
    device->GetAttribute("ReceiveErrorModel", previous);
    auto error = ns3::CreateObject<Blackhole>(); // No RNG or random-valued attributes.
    error->original = previous.Get<ns3::ErrorModel>();
    device->SetAttribute("ReceiveErrorModel", ns3::PointerValue(error));
    state.gates.push_back(Gate{id,port,device,error});
    selectedChannels.push_back(device->GetChannel());
    std::cout << "DCI_PM_FAULT_TARGET node=" << id << " port=" << port
      << " path_index=" << state.index << '\n';
  }
  NS_ABORT_MSG_IF(selectedChannels[0] != selectedChannels[1], "Fault endpoints do not share the same physical link");
  std::cout << "DCI_PM_FAULT_CONFIG enabled=1 path_index=" << state.index
    << " start_us=" << state.startUs << " end_us=" << state.endUs
    << " model=receive_blackhole business_routing=unchanged rng=none\n";
  ns3::Simulator::Schedule(ns3::MicroSeconds(state.startUs), &Toggle, true);
  ns3::Simulator::Schedule(ns3::MicroSeconds(state.endUs), &Toggle, false);
}
inline void Dump() {
  auto& state = Get();
  if (!state.enabled || state.dumped) return;
  state.dumped = true;
  for (const auto& gate : state.gates)
    std::cout << "DCI_PM_FAULT_DROPS node=" << gate.node << " port=" << gate.port
      << " data_packets=" << gate.error->dataDrops << " data_bytes=" << gate.error->dataBytes
      << " probe_packets=" << gate.error->probeDrops << " other_packets=" << gate.error->otherDrops
      << " link_up=" << gate.device->IsLinkUp() << '\n';
  for (const auto& item : state.traffic) {
    const auto& c = item.second;
    std::cout << "DCI_PM_FAULT_TRAFFIC node=" << c.node << " port=" << c.port
      << " data_before=" << c.data[0] << " data_during=" << c.data[1] << " data_after=" << c.data[2]
      << " probe_before=" << c.probes[0] << " probe_during=" << c.probes[1]
      << " probe_after=" << c.probes[2] << '\n';
  }
  std::cout << "DCI_PM_FAULT_END events=" << state.events
    << " time_ns=" << ns3::Simulator::Now().GetNanoSeconds() << '\n';
}
} // namespace DciPmFault
#endif
