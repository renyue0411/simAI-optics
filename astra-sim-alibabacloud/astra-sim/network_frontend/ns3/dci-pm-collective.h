// SPDX-License-Identifier: GPL-2.0-only
// Experiment glue: private gateway control routes and an optional probe service.
#ifndef SIMAI_DCI_PM_COLLECTIVE_H
#define SIMAI_DCI_PM_COLLECTIVE_H
#include "ns3/abort.h"
#include "ns3/dci-path-manager-app.h"
#include "ns3/ipv4.h"
#include "ns3/node-container.h"
#include "ns3/qbb-net-device.h"
#include "ns3/simulator.h"
#include "ns3/switch-node.h"
#include "dci-pm-fault-experiment.h" // DCI_PM_FAULT_V1
#include <cstdlib>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <vector>

namespace DciPmCollective {
struct ServiceState {
  bool installed = false, enabled = false, dumped = false, probeEnabled = true;
  uint32_t intervalUs = 1000, timeoutUs = 10000, serviceUs = 5, observeUs = 1000;
  ns3::EventId observer;
  std::vector<ns3::Ptr<ns3::DciPathManagerApp>> apps;
};
inline ServiceState& State() { static ServiceState state; return state; }
inline uint32_t Number(const std::string& value, uint32_t maximum) {
  NS_ABORT_MSG_IF(value.empty(), "Empty PM integer");
  uint64_t result = 0;
  for (char c : value) {
    NS_ABORT_MSG_IF(c < '0' || c > '9', "Invalid PM integer: " << value);
    result = result * 10 + (c - '0');
    NS_ABORT_MSG_IF(result > maximum, "PM integer exceeds limit: " << value);
  }
  return result;
}
inline uint32_t Option(const char* name, uint32_t fallback, uint32_t maximum) {
  const char* value = std::getenv(name);
  return value ? Number(value, maximum) : fallback;
}
inline ns3::Ipv4Address Address(const std::string& text) {
  std::istringstream input(text);
  std::string token;
  uint32_t result = 0, count = 0;
  while (std::getline(input, token, '.')) {
    NS_ABORT_MSG_IF(++count > 4, "Invalid PM IPv4 address");
    result = (result << 8) | Number(token, 255);
  }
  NS_ABORT_MSG_IF(count != 4 || text.back() == '.' || (result >> 24) != 10,
                  "PM experiment control address must be a private 10.x.x.x IPv4 address");
  return ns3::Ipv4Address(result);
}
inline void Observe(bool again) {
  auto& state = State();
  for (const auto& app : state.apps)
    for (const auto& s : app->GetSnapshot())
      std::cout << "DCI_PM_OBSERVE node=" << app->GetNode()->GetId()
        << " path=" << s.pathId << " peer=" << s.peerAddress
        << " interface=" << s.interface << " time_ns=" << ns3::Simulator::Now().GetNanoSeconds()
        << " state=" << ns3::DciPathManagerApp::StatusName(s.status)
        << " has_rtt=" << s.hasRtt << " samples=" << s.samples
        << " control_rtt_ns=" << s.latestRtt.GetNanoSeconds()
        << " last_reply_ns=" << s.lastReply.GetNanoSeconds() << '\n';
  if (again && state.observeUs)
    state.observer = ns3::Simulator::Schedule(ns3::MicroSeconds(state.observeUs), &Observe, true);
}

// The link map belongs to topology initialization, not the manager. It supplies
// local direct-neighbor interfaces only; no delay/rate/health oracle is queried.
template <typename LinkMap>
inline void Install(const ns3::NodeContainer& nodes, const LinkMap& links) {
  auto& state = State();
  NS_ABORT_MSG_IF(state.installed, "PM collective glue installed twice");
  state.installed = true;
  state.enabled = Option("SIMAI_PM_ENABLE", 0, 1) == 1;
  state.probeEnabled = Option("SIMAI_PM_PROBE_ENABLE", 1, 1) == 1;
  state.intervalUs = Option("SIMAI_PM_INTERVAL_US", 1000, 10000000);
  state.timeoutUs = Option("SIMAI_PM_TIMEOUT_US", 10000, 10000000);
  state.serviceUs = Option("SIMAI_PM_SERVICE_US", 5, 1000000);
  state.observeUs = Option("SIMAI_PM_OBSERVE_US", 1000, 10000000);
  NS_ABORT_MSG_IF(!state.intervalUs || !state.timeoutUs, "PM interval/timeout must be positive");
  std::cout << "DCI_PM_COEXIST_CONFIG version=1 enabled=" << state.enabled
    << " probe_enable=" << state.probeEnabled
    << " interval_us=" << state.intervalUs << " timeout_us=" << state.timeoutUs
    << " service_us=" << state.serviceUs << " observe_us=" << state.observeUs
    << " candidates=route_snapshot control_queue=0 traffic_start=unchanged\n";
  DciPmFault::Install(nodes, links); // DCI_PM_FAULT_V1, also in PM-off controls.
  if (!state.enabled) return; // Disabled baseline installs no routes/apps/events.
  const char* raw = std::getenv("SIMAI_PM_GATEWAYS");
  NS_ABORT_MSG_IF(!raw || !*raw, "Set SIMAI_PM_GATEWAYS, e.g. 6@10.255.0.6,7@10.255.0.7");
  std::map<uint32_t, ns3::Ipv4Address> gateways;
  std::set<uint32_t> addresses;
  std::istringstream input(raw);
  std::string token;
  while (std::getline(input, token, ',')) {
    const auto at = token.find('@');
    NS_ABORT_MSG_IF(at == std::string::npos, "PM gateway must be node@IPv4");
    const uint32_t id = Number(token.substr(0, at), 65535);
    const auto address = Address(token.substr(at + 1));
    NS_ABORT_MSG_IF(id >= nodes.GetN() || nodes.Get(id)->GetId() != id ||
      !ns3::DynamicCast<ns3::SwitchNode>(nodes.Get(id)), "PM gateway must identify an existing SwitchNode");
    NS_ABORT_MSG_IF(!gateways.emplace(id, address).second || !addresses.insert(address.Get()).second,
                    "Duplicate PM gateway or control address");
  }
  NS_ABORT_MSG_IF(gateways.size() < 2, "PM requires at least two configured gateways");
  // Dedicated control identities must not alias any existing business/link IP.
  for (uint32_t i = 0; i < nodes.GetN(); ++i) {
    auto ip = nodes.Get(i)->GetObject<ns3::Ipv4>();
    if (!ip) continue;
    for (uint32_t j = 0; j < ip->GetNInterfaces(); ++j)
      for (uint32_t k = 0; k < ip->GetNAddresses(j); ++k)
        NS_ABORT_MSG_IF(addresses.count(ip->GetAddress(j, k).GetLocal().Get()),
                        "PM control address collides with an existing address");
  }
  uint32_t directedCandidates = 0;
  for (const auto& local : gateways) {
    auto node = nodes.Get(local.first);
    auto sw = ns3::DynamicCast<ns3::SwitchNode>(node);
    auto ip = node->GetObject<ns3::Ipv4>();
    NS_ABORT_MSG_IF(!ip || !ip->GetNInterfaces(), "PM gateway lacks an IP loopback");
    NS_ABORT_MSG_IF(!ip->AddAddress(0, ns3::Ipv4InterfaceAddress(local.second,
      ns3::Ipv4Mask("255.255.255.255"))), "Cannot assign PM control identity");
    auto app = ns3::CreateObject<ns3::DciPathManagerApp>();
    node->AddApplication(app);
    ns3::DciPathManagerApp::Settings settings;
    settings.probeInterval = ns3::MicroSeconds(state.intervalUs);
    settings.timeout = ns3::MicroSeconds(state.timeoutUs);
    settings.processingDelay = ns3::MicroSeconds(state.serviceUs);
    settings.probeEnabled = state.probeEnabled;
    app->Configure(settings);
    const auto localLinks = links.find(node);
    NS_ABORT_MSG_IF(localLinks == links.end(), "No local adjacency for PM gateway");
    uint32_t peers = 0;
    for (const auto& remote : gateways) {
      if (remote.first == local.first) continue;
      const auto adjacency = localLinks->second.find(nodes.Get(remote.first));
      if (adjacency == localLinks->second.end()) continue;
      std::set<uint32_t> ports;
      for (const auto& link : adjacency->second) if (link.up) ports.insert(link.idx);
      if (ports.empty()) continue;
      NS_ABORT_MSG_IF(sw->GetRoutingTable().count(remote.second.Get()),
                      "PM control destination already has a forwarding entry");
      for (uint32_t port : ports) {
        NS_ABORT_MSG_IF(port >= node->GetNDevices() ||
          !ns3::DynamicCast<ns3::QbbNetDevice>(node->GetDevice(port)), "PM WAN interface is not Qbb");
        auto destination = remote.second; // Existing API takes a non-const reference.
        sw->AddTableEntry(destination, port);
        std::cout << "DCI_PM_CONTROL_ROUTE node=" << local.first << " peer_node=" << remote.first
          << " destination=" << remote.second << " interface=" << port
          << " source=experiment_direct_adjacency\n";
      }
      const uint32_t added = app->AddPeer(local.second, remote.second, ports);
      NS_ABORT_MSG_IF(added != ports.size(), "PM route snapshot differs from configured WAN adjacency");
      directedCandidates += added; ++peers;
    }
    NS_ABORT_MSG_IF(!peers, "PM gateway has no directly connected configured peer");
    app->SetStartTime(ns3::Seconds(0));
    state.apps.push_back(app);
  }
  std::cout << "DCI_PM_COEXIST_INSTALLED apps=" << state.apps.size()
    << " directed_candidates=" << directedCandidates << " start_ns=0\n";
  if (state.observeUs)
    state.observer = ns3::Simulator::Schedule(ns3::MicroSeconds(state.observeUs), &Observe, true);
}
inline void Dump() {
  auto& state = State();
  if (!state.installed || state.dumped) return;
  state.dumped = true;
  DciPmFault::Dump(); // DCI_PM_FAULT_V1
  ns3::Simulator::Cancel(state.observer);
  Observe(false);
  uint64_t txPackets = 0, txBytes = 0, rxPackets = 0, timeouts = 0;
  uint32_t candidates = 0, usable = 0;
  for (const auto& app : state.apps) {
    app->PrintStats();
    for (const auto& s : app->GetSnapshot()) {
      ++candidates; usable += s.IsUsable();
      txPackets += s.txPackets; txBytes += s.txBytes;
      rxPackets += s.rxPackets; timeouts += s.timeouts;
    }
  }
  std::cout << "DCI_PM_COEXIST_END enabled=" << state.enabled << " apps=" << state.apps.size()
    << " candidates=" << candidates << " usable=" << usable << " tx_packets=" << txPackets
    << " tx_bytes=" << txBytes << " rx_packets=" << rxPackets << " timeouts=" << timeouts
    << " time_ns=" << ns3::Simulator::Now().GetNanoSeconds()
    << " scope=both_gateways_all_control_transmissions\n";
}
} // namespace DciPmCollective
#endif
