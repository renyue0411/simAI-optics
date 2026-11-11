// SPDX-License-Identifier: GPL-2.0-only
#ifndef SIMAI_DCI_WAN_PROFILE_H
#define SIMAI_DCI_WAN_PROFILE_H
#include "ns3/abort.h"
#include "ns3/boolean.h"
#include "ns3/qbb-net-device.h"
#include "ns3/switch-node.h"
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <map>
#include <set>
#include <string>
namespace DciWanProfile {
// DCI_WAN_PROFILE_V1: initialization and observation, never a scheduling oracle.
inline uint64_t Uint(const char* name, uint64_t fallback, uint64_t maximum) {
  const char* raw = std::getenv(name);
  if (!raw) return fallback;
  NS_ABORT_MSG_IF(!*raw, "Empty WAN profile option: " << name);
  uint64_t value = 0;
  for (const char* p = raw; *p; ++p) {
    NS_ABORT_MSG_IF(*p < '0' || *p > '9' || value > maximum / 10,
                    "Invalid WAN profile option: " << name);
    value = value * 10 + (*p - '0');
    NS_ABORT_MSG_IF(value > maximum, "WAN profile option out of range: " << name);
  }
  return value;
}
struct Settings {
  bool enabled; uint32_t bufferBytes, kminKb, kmaxKb; double pmax;
};
inline const Settings& Config() {
  static const Settings settings = []() {
    Settings s;
    s.enabled = Uint("SIMAI_WAN_PROFILE", 0, 1);
    const uint64_t mib = Uint("SIMAI_DCI_BUFFER_MIB", 160, 4095);
    NS_ABORT_MSG_IF(!mib, "DCI buffer must be positive");
    s.bufferBytes = mib * 1048576;
    s.kminKb = Uint("SIMAI_WAN_ECN_KMIN_KB", 1000, 4294967);
    s.kmaxKb = Uint("SIMAI_WAN_ECN_KMAX_KB", 20000, 4294967);
    s.pmax = 0.15;
    if (const char* raw = std::getenv("SIMAI_WAN_ECN_PMAX")) {
      char* end = nullptr; errno = 0; s.pmax = std::strtod(raw, &end);
      NS_ABORT_MSG_IF(errno || end == raw || *end || !std::isfinite(s.pmax) ||
                      s.pmax < 0 || s.pmax > 1, "Invalid WAN ECN pmax");
    }
    NS_ABORT_MSG_IF(s.kminKb >= s.kmaxKb || uint64_t(s.kmaxKb) * 1000 >= s.bufferBytes,
                    "Require 0 <= WAN kmin < kmax < DCI buffer");
    return s;
  }();
  return settings;
}
struct Gateway { ns3::Ptr<ns3::SwitchNode> sw; std::set<uint32_t> wan; };
inline std::map<uint32_t, Gateway>& Gateways() {
  static std::map<uint32_t, Gateway> gateways; return gateways;
}
inline void Apply(ns3::Ptr<ns3::SwitchNode> sw, const std::set<uint32_t>& wan) {
  const auto& c = Config();
  NS_ABORT_MSG_IF(!sw || wan.empty() || Gateways().count(sw->GetId()),
                  "Invalid/duplicate WAN profile gateway");
  Gateways()[sw->GetId()] = {sw, wan};
  if (c.enabled) {
    sw->SetAttribute("EcnEnabled", ns3::BooleanValue(true));
    for (uint32_t port : wan) {
      NS_ABORT_MSG_IF(port == 0 || port >= sw->GetNDevices(), "Invalid WAN profile port");
      auto dev = ns3::DynamicCast<ns3::QbbNetDevice>(sw->GetDevice(port));
      NS_ABORT_MSG_IF(!dev, "WAN port is not Qbb");
      dev->SetAttribute("QbbEnabled", ns3::BooleanValue(false));
      sw->m_mmu->ConfigDciWanPort(port);
      sw->m_mmu->ConfigEcn(port, c.kminKb, c.kmaxKb, c.pmax);
    }
    sw->m_mmu->ConfigNPort(sw->GetNDevices() - 1);
    sw->m_mmu->ConfigBufferSize(c.bufferBytes);
    NS_ABORT_MSG_IF(sw->m_mmu->DciSharedCapacity() == 0, "DCI buffer cannot cover LAN reservation");
  }
  ns3::BooleanValue ecn; sw->GetAttribute("EcnEnabled", ecn);
  std::cout << "DCI_WAN_PROFILE_CONFIG node=" << sw->GetId() << " enabled=" << c.enabled
    << " ecn_enabled=" << ecn.Get() << " buffer_bytes=" << sw->m_mmu->buffer_size
    << " admission=" << (c.enabled ? "bounded_shared_wan" : "legacy")
    << " q0_scope=separate_control_queue\n";
  for (uint32_t port = 1; port < sw->GetNDevices(); ++port) {
    auto dev = ns3::DynamicCast<ns3::QbbNetDevice>(sw->GetDevice(port));
    NS_ABORT_MSG_IF(!dev, "Gateway port is not Qbb");
    std::cout << "DCI_WAN_PROFILE_PORT node=" << sw->GetId() << " port=" << port
      << " wan=" << wan.count(port) << " pfc=" << dev->IsPfcEnabled()
      << " headroom_bytes=" << sw->m_mmu->headroom[port]
      << " kmin_bytes=" << sw->m_mmu->kmin[port]
      << " kmax_bytes=" << sw->m_mmu->kmax[port]
      << " pmax=" << sw->m_mmu->pmax[port] << '\n';
  }
}
inline void Dump() {
  const auto& c = Config();
  NS_ABORT_MSG_IF(c.enabled && Gateways().size() != 2,
                  "WAN profile requires initialized two-gateway topology mode");
  for (const auto& entry : Gateways()) {
    auto sw = entry.second.sw; auto mmu = sw->m_mmu;
    uint64_t egress = 0;
    for (uint32_t port = 1; port < sw->GetNDevices(); ++port)
      for (uint32_t q = 1; q < ns3::SwitchMmu::qCnt; ++q) egress += mmu->egress_bytes[port][q];
    if (c.enabled) {
      NS_ABORT_MSG_IF(egress != mmu->m_dciDataUsed || mmu->m_dciDataPeak > mmu->buffer_size ||
                      mmu->shared_used_bytes > mmu->DciSharedCapacity(), "DCI DATA pool accounting mismatch");
    }
    std::cout << "DCI_WAN_POOL_STATS node=" << entry.first << " enabled=" << c.enabled
      << " tracked=" << mmu->m_dciWanPool << " capacity_bytes=" << mmu->buffer_size
      << " shared_capacity_bytes=" << mmu->DciSharedCapacity()
      << " data_used_bytes=" << mmu->m_dciDataUsed << " data_peak_bytes=" << mmu->m_dciDataPeak
      << " shared_used_bytes=" << mmu->shared_used_bytes
      << " admission_drops=" << mmu->m_dciAdmissionDrops << " egress_data_bytes=" << egress
      << " scope=all_nonzero_priority_packets_at_gateway\n";
    for (uint32_t port = 1; port < sw->GetNDevices(); ++port) {
      auto dev = ns3::DynamicCast<ns3::QbbNetDevice>(sw->GetDevice(port));
      uint32_t paused = 0;
      for (uint32_t q = 0; q < ns3::SwitchMmu::qCnt; ++q) if (dev->IsPfcPaused(q)) ++paused;
      std::cout << "DCI_WAN_PFC_STATS node=" << entry.first << " port=" << port
        << " wan=" << entry.second.wan.count(port) << " enabled=" << dev->IsPfcEnabled()
        << " tx_packets=" << dev->m_dciPfcTx << " rx_packets=" << dev->m_dciPfcRx
        << " rx_ignored=" << dev->m_dciPfcIgnored << " tx_suppressed=" << dev->m_dciPfcTxSuppressed
        << " paused_queues=" << paused << '\n';
    }
  }
}
} // namespace DciWanProfile
#endif
