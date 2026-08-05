'use strict';

const state = {
  experiments: [],
  selectedName: null,
  data: null,
  experimentFilter: '',
  flowFilter: '',
  tableFilter: '',
  sportFilter: 'all',
  flowLimit: '24',
  isolatedFlow: null,
  injectionNode: null,
  flowSort: { key: 'fct_us', direction: 'desc' },
  topology: { scale: 1, tx: 0, ty: 0, draggingNode: null, panning: false, lastX: 0, lastY: 0, positions: {} },
};

const palette = [
  '#486b82', '#66869a', '#7d99aa', '#95aab7', '#577d8a', '#78909c',
  '#3f657c', '#6f8b9c', '#899eaa', '#5f778a', '#7597a4', '#4e7184',
  '#8ca2af', '#607f93', '#7792a2', '#98abb6', '#536d7e', '#6d8796',
  '#829bab', '#456579', '#6d8b9f', '#849aa6', '#59798c', '#7591a0',
];

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? '—').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
const fmt = (value, digits = 2) => value === null || value === undefined || Number.isNaN(Number(value)) ? '—' : Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
const formatDuration = seconds => seconds === null || seconds === undefined ? '—' : `${fmt(seconds, 3)} s`;
const formatUtc = value => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
};

async function fetchJson(url) {
  const response = await fetch(url, { cache: 'no-store' });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function setLoading(show) {
  $('loadingOverlay').classList.toggle('hidden', !show);
}

async function loadExperiments(preferredName = null) {
  try {
    const payload = await fetchJson('/api/experiments');
    state.experiments = payload.experiments || [];
    $('runsPath').textContent = payload.runs_dir || '';
    renderExperimentList();
    const target = preferredName && state.experiments.some(e => e.name === preferredName)
      ? preferredName
      : state.selectedName && state.experiments.some(e => e.name === state.selectedName)
        ? state.selectedName
        : state.experiments[0]?.name;
    if (target) await selectExperiment(target);
    else renderEmptyDashboard('No experiments found under the configured runs directory.');
  } catch (error) {
    renderEmptyDashboard(error.message);
  }
}

function renderExperimentList() {
  const query = state.experimentFilter.toLowerCase();
  const items = state.experiments.filter(item => item.name.toLowerCase().includes(query));
  $('experimentList').innerHTML = items.map(item => `
    <button class="experiment-item ${item.name === state.selectedName ? 'active' : ''}" data-experiment="${escapeHtml(item.name)}">
      <span class="status-dot ${escapeHtml(item.status)}"></span>
      <span>
        <span class="experiment-name">${escapeHtml(item.name)}</span>
        <span class="experiment-detail">${escapeHtml(item.status)} · ${formatDuration(item.duration_seconds)} · ${escapeHtml(formatUtc(item.started_at))}</span>
      </span>
    </button>
  `).join('') || '<div class="experiment-detail" style="padding:14px">No matching experiments.</div>';
  document.querySelectorAll('[data-experiment]').forEach(button => {
    button.addEventListener('click', () => selectExperiment(button.dataset.experiment));
  });
}

async function selectExperiment(name) {
  if (!name) return;
  state.selectedName = name;
  state.isolatedFlow = null;
  state.injectionNode = null;
  state.topology = { scale: 1, tx: 0, ty: 0, draggingNode: null, panning: false, lastX: 0, lastY: 0, positions: {} };
  renderExperimentList();
  setLoading(true);
  try {
    state.data = await fetchJson(`/api/experiment/${encodeURIComponent(name)}`);
    renderDashboard();
  } catch (error) {
    renderEmptyDashboard(error.message);
  } finally {
    setLoading(false);
  }
}

function renderEmptyDashboard(message) {
  $('experimentTitle').textContent = 'No experiment selected';
  $('experimentMeta').textContent = message;
  $('configCards').innerHTML = '';
  $('resultCards').innerHTML = '';
  $('topologyCanvas').innerHTML = emptyState(message);
  $('throughputChart').innerHTML = emptyState(message);
  setLoading(false);
}

function renderDashboard() {
  const data = state.data;
  $('experimentTitle').textContent = data.name;
  const manifest = data.manifest || {};
  $('experimentMeta').textContent = `${formatUtc(manifest.started_at_utc)} · ${manifest.git?.simai_commit?.slice(0, 8) || 'no commit'} / ${manifest.git?.ns3_backend_commit?.slice(0, 8) || 'no ns-3 commit'}`;
  const status = String(data.result?.status || 'unknown').toLowerCase();
  $('resultBadge').textContent = status;
  $('resultBadge').className = `status-badge ${status}`;
  renderWarnings();
  renderConfigCards();
  renderResultCards();
  renderTopology();
  initializeFlowFilters();
  renderFlows();
  renderSchedule();
  renderInjection();
  renderOcsStats();
  renderRetransmission();
}

function renderWarnings() {
  const warnings = state.data?.warnings || [];
  const panel = $('warningPanel');
  panel.classList.toggle('hidden', warnings.length === 0);
  panel.innerHTML = warnings.length ? `<ul>${warnings.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : '';
}

function renderConfigCards() {
  $('configCards').innerHTML = (state.data.config_cards || []).map(card => `
    <div class="metric-card">
      <span class="label">${escapeHtml(card.label)}</span>
      <span class="value">${escapeHtml(card.value ?? '—')}</span>
    </div>
  `).join('');
}

function renderResultCards() {
  const result = state.data.result || {};
  const flows = state.data.flows?.summary || {};
  const cards = [
    ['Status', result.status],
    ['Runtime', result.wall_duration_seconds !== undefined ? `${fmt(result.wall_duration_seconds, 3)} s` : '—'],
    ['Completion Time', result.all_passes_finished_ms !== undefined ? `${fmt(result.all_passes_finished_ms, 3)} ms` : result.total_time_us !== undefined ? `${fmt(result.total_time_us / 1000, 3)} ms` : '—'],
    ['Streams', result.streams_finished !== undefined ? `${result.streams_finished}/${result.streams_injected ?? '—'}` : '—'],
    ['Flows', flows.count ?? '—'],
    ['Total Flow Bytes', flows.total_bytes_human ?? '—'],
    ['Average FCT', flows.avg_fct_us !== undefined ? `${fmt(flows.avg_fct_us, 3)} µs` : '—'],
    ['Maximum FCT', flows.max_fct_us !== undefined ? `${fmt(flows.max_fct_us, 3)} µs` : '—'],
    ['Return Code', result.return_code ?? '—'],
    ['Finished', result.finished_at_utc ? formatUtc(result.finished_at_utc) : '—'],
  ];
  $('resultCards').innerHTML = cards.map(([label, value]) => `
    <div class="result-card"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(value)}</div></div>
  `).join('');
}

function emptyState(message) {
  return `<div style="display:flex;align-items:center;justify-content:center;min-height:160px;color:#71848f;font-size:12px;padding:20px;text-align:center">${escapeHtml(message)}</div>`;
}

function inferScaleOutHierarchy(nodes, links) {
  const nodeById = new Map(nodes.map(node => [Number(node.id), node]));
  const scaleOutNodes = nodes.filter(node => !['gpu', 'nvswitch'].includes(node.type));
  const scaleOutIds = new Set(scaleOutNodes.map(node => Number(node.id)));
  const adjacency = new Map(nodes.map(node => [Number(node.id), new Set()]));

  links.forEach(link => {
    const source = Number(link.source);
    const target = Number(link.target);
    const sourceType = nodeById.get(source)?.type;
    const targetType = nodeById.get(target)?.type;
    const isScaleUp = new Set([sourceType, targetType]).has('gpu')
      && new Set([sourceType, targetType]).has('nvswitch');
    if (isScaleUp) return;
    adjacency.get(source)?.add(target);
    adjacency.get(target)?.add(source);
  });

  const depth = new Map();
  const queue = [];
  nodes.filter(node => node.type === 'gpu').forEach(gpu => {
    (adjacency.get(Number(gpu.id)) || []).forEach(neighbor => {
      if (!scaleOutIds.has(neighbor) || depth.has(neighbor)) return;
      depth.set(neighbor, 1);
      queue.push(neighbor);
    });
  });

  while (queue.length) {
    const current = queue.shift();
    const nextDepth = depth.get(current) + 1;
    (adjacency.get(current) || []).forEach(neighbor => {
      if (!scaleOutIds.has(neighbor)) return;
      if (!depth.has(neighbor) || nextDepth < depth.get(neighbor)) {
        depth.set(neighbor, nextDepth);
        queue.push(neighbor);
      }
    });
  }

  // Fallback for incomplete topology snapshots. Connectivity remains the primary source.
  scaleOutNodes.forEach(node => {
    const id = Number(node.id);
    if (depth.has(id)) return;
    const gpuFacing = [...(adjacency.get(id) || [])]
      .some(neighbor => nodeById.get(neighbor)?.type === 'gpu');
    if (gpuFacing || node.type === 'leaf_ocs') depth.set(id, 1);
    else if (node.type === 'core_ocs') depth.set(id, 2);
  });

  let changed = true;
  while (changed) {
    changed = false;
    scaleOutNodes.forEach(node => {
      const id = Number(node.id);
      if (depth.has(id)) return;
      const known = [...(adjacency.get(id) || [])]
        .filter(neighbor => scaleOutIds.has(neighbor) && depth.has(neighbor))
        .map(neighbor => depth.get(neighbor));
      if (known.length) {
        depth.set(id, Math.min(...known) + 1);
        changed = true;
      }
    });
  }

  scaleOutNodes.forEach(node => {
    const id = Number(node.id);
    if (!depth.has(id)) depth.set(id, 1);
  });

  const tiers = new Map();
  scaleOutNodes.forEach(node => {
    const d = depth.get(Number(node.id));
    if (!tiers.has(d)) tiers.set(d, []);
    tiers.get(d).push(node);
  });
  const maxDepth = Math.max(1, ...depth.values());
  return { adjacency, depth, tiers, maxDepth };
}

function defaultTopologyPositions(nodes, servers, links) {
  const width = 1500;
  const positions = {};

  // Keep every server as one compact scale-up domain: GPUs on top, NVSwitch below.
  const gpuY = 545;
  const nvswitchY = 690;
  if (servers?.length) {
    servers.forEach((server, serverIndex) => {
      const center = 160 + serverIndex * ((width - 320) / Math.max(servers.length - 1, 1));
      const gpus = server.gpus || [];
      gpus.forEach((gpu, index) => {
        positions[gpu] = { x: center + (index - (gpus.length - 1) / 2) * 55, y: gpuY };
      });
      if (server.nvswitch !== undefined) positions[server.nvswitch] = { x: center, y: nvswitchY };
    });
  } else {
    const gpus = nodes.filter(node => node.type === 'gpu');
    const margin = 90;
    const step = gpus.length <= 1 ? 0 : (width - margin * 2) / (gpus.length - 1);
    gpus.forEach((node, index) => {
      positions[node.id] = { x: gpus.length === 1 ? width / 2 : margin + step * index, y: gpuY };
    });
    const nvswitches = nodes.filter(node => node.type === 'nvswitch');
    nvswitches.forEach((node, index) => {
      positions[node.id] = { x: 160 + index * ((width - 320) / Math.max(nvswitches.length - 1, 1)), y: nvswitchY };
    });
  }

  const hierarchy = inferScaleOutHierarchy(nodes, links || []);
  const scaleOutBottom = 350;
  const tierGap = hierarchy.maxDepth === 2
    ? 185
    : hierarchy.maxDepth > 2
      ? Math.min(135, 230 / Math.max(hierarchy.maxDepth - 1, 1))
      : 0;

  const scoreFromNeighbors = (node, targetDepth) => {
    const xs = [...(hierarchy.adjacency.get(Number(node.id)) || [])]
      .filter(neighbor => {
        const neighborNode = nodes.find(item => Number(item.id) === neighbor);
        if (!neighborNode) return false;
        if (targetDepth === 0) return neighborNode.type === 'gpu';
        return hierarchy.depth.get(neighbor) === targetDepth;
      })
      .map(neighbor => positions[neighbor]?.x)
      .filter(value => Number.isFinite(value));
    return xs.length ? xs.reduce((sum, value) => sum + value, 0) / xs.length : Number(node.id);
  };

  [...hierarchy.tiers.keys()].sort((a, b) => a - b).forEach(depth => {
    const row = hierarchy.tiers.get(depth);
    // Keep switches in deterministic numeric order within each inferred tier.
    row.sort((a, b) => Number(a.id) - Number(b.id));
    const margin = 110;
    const step = row.length <= 1 ? 0 : (width - margin * 2) / (row.length - 1);
    const y = scaleOutBottom - (depth - 1) * tierGap;
    row.forEach((node, index) => {
      positions[node.id] = { x: row.length === 1 ? width / 2 : margin + step * index, y };
    });
  });

  return positions;
}

function renderTopology() {
  const topology = state.data.topology || {};
  const nodes = topology.nodes || [];
  const links = topology.links || [];
  if (!nodes.length) {
    $('topologyCanvas').innerHTML = emptyState('Topology data is unavailable.');
    return;
  }
  if (!Object.keys(state.topology.positions).length) {
    state.topology.positions = defaultTopologyPositions(nodes, topology.servers || [], links);
  }

  const width = 1500, height = 800;
  const position = id => state.topology.positions[id] || { x: width / 2, y: height / 2 };
  const nodeById = new Map(nodes.map(node => [Number(node.id), node]));
  const scaleOutHierarchy = inferScaleOutHierarchy(nodes, links);

  const boundsFor = (ids, padding) => {
    const valid = ids.filter(id => id !== undefined && id !== null && state.topology.positions[id]);
    if (!valid.length) return null;
    const xs = valid.map(id => position(id).x);
    const ys = valid.map(id => position(id).y);
    return {
      x: Math.min(...xs) - padding.left,
      y: Math.min(...ys) - padding.top,
      width: Math.max(...xs) - Math.min(...xs) + padding.left + padding.right,
      height: Math.max(...ys) - Math.min(...ys) + padding.top + padding.bottom,
    };
  };

  const scaleOutIds = nodes
    .filter(node => ['core_ocs', 'leaf_ocs', 'eps'].includes(node.type))
    .map(node => node.id);
  const scaleUpIds = nodes
    .filter(node => ['gpu', 'nvswitch'].includes(node.type))
    .map(node => node.id);
  const scaleOutBounds = boundsFor(scaleOutIds, { left: 70, right: 70, top: 70, bottom: 65 });
  const scaleUpBounds = boundsFor(scaleUpIds, { left: 70, right: 70, top: 95, bottom: 70 });

  const domainMarkup = [
    scaleOutBounds ? `<g class="topology-domain-group">
      <rect class="topology-domain scale-out-domain" x="${scaleOutBounds.x}" y="${scaleOutBounds.y}" width="${scaleOutBounds.width}" height="${scaleOutBounds.height}"></rect>
      <text x="${scaleOutBounds.x + 18}" y="${scaleOutBounds.y + 25}" class="topology-domain-title">SCALE-OUT</text>
    </g>` : '',
    scaleUpBounds ? `<g class="topology-domain-group">
      <rect class="topology-domain scale-up-domain" x="${scaleUpBounds.x}" y="${scaleUpBounds.y}" width="${scaleUpBounds.width}" height="${scaleUpBounds.height}"></rect>
      <text x="${scaleUpBounds.x + 18}" y="${scaleUpBounds.y + 25}" class="topology-domain-title">SCALE-UP</text>
    </g>` : '',
  ].join('');

  const serverBoxes = (topology.servers || []).map(server => {
    const memberIds = [...(server.gpus || []), server.nvswitch]
      .filter(id => id !== undefined && id !== null && state.topology.positions[id]);
    if (!memberIds.length) return '';
    const box = boundsFor(memberIds, { left: 38, right: 38, top: 48, bottom: 48 });
    return `<g class="server-domain-group">
      <rect class="server-box" x="${box.x}" y="${box.y}" width="${box.width}" height="${box.height}"></rect>
      <text x="${box.x + 12}" y="${box.y + 19}" class="server-title">SERVER ${server.id}</text>
    </g>`;
  }).join('');

  // Infer scale-out planes from GPU-side <nic>-<plane> tokens,
  // then propagate the plane identity through the scale-out fabric.
  const parsePortPlane = port => {
    const match = String(port ?? '').match(/^\d+-(\d+)$/);
    return match ? Number(match[1]) : null;
  };

  const fabricNodePlane = new Map();

  const setFabricNodePlane = (nodeId, planeId) => {
    const current = fabricNodePlane.get(nodeId);

    if (current === undefined) {
      fabricNodePlane.set(nodeId, planeId);
    } else if (current !== planeId) {
      // The node is shared by multiple planes; its integer-port links
      // cannot be classified from node membership alone.
      fabricNodePlane.set(nodeId, -1);
    }
  };

  // Seed the first-hop Leaf OCS/EPS nodes from explicit GPU port tokens.
  links.forEach(link => {
    const explicitPlane =
      parsePortPlane(link.source_port) ??
      parsePortPlane(link.target_port);

    if (explicitPlane === null) return;

    const sourceId = Number(link.source);
    const targetId = Number(link.target);
    const sourceType = nodeById.get(sourceId)?.type;
    const targetType = nodeById.get(targetId)?.type;

    if (!['gpu', 'nvswitch'].includes(sourceType)) {
      setFabricNodePlane(sourceId, explicitPlane);
    }

    if (!['gpu', 'nvswitch'].includes(targetType)) {
      setFabricNodePlane(targetId, explicitPlane);
    }
  });

  // Propagate the plane through Leaf OCS -> Core OCS/EPS links.
  let planeChanged = true;

  while (planeChanged) {
    planeChanged = false;

    links.forEach(link => {
      const sourceId = Number(link.source);
      const targetId = Number(link.target);
      const sourceType = nodeById.get(sourceId)?.type;
      const targetType = nodeById.get(targetId)?.type;

      if (
        ['gpu', 'nvswitch'].includes(sourceType) ||
        ['gpu', 'nvswitch'].includes(targetType)
      ) {
        return;
      }

      const sourcePlane = fabricNodePlane.get(sourceId);
      const targetPlane = fabricNodePlane.get(targetId);

      if (
        sourcePlane !== undefined &&
        sourcePlane >= 0 &&
        targetPlane === undefined
      ) {
        fabricNodePlane.set(targetId, sourcePlane);
        planeChanged = true;
      } else if (
        targetPlane !== undefined &&
        targetPlane >= 0 &&
        sourcePlane === undefined
      ) {
        fabricNodePlane.set(sourceId, targetPlane);
        planeChanged = true;
      }
    });
  }

  const inferLinkPlane = link => {
    // Endpoint-facing links contain the plane explicitly.
    const explicitPlane =
      parsePortPlane(link.source_port) ??
      parsePortPlane(link.target_port);

    if (explicitPlane !== null) return explicitPlane;

    // Fabric-internal links inherit the plane from their connected devices.
    const sourcePlane = fabricNodePlane.get(Number(link.source));
    const targetPlane = fabricNodePlane.get(Number(link.target));

    if (
      sourcePlane !== undefined &&
      sourcePlane >= 0 &&
      targetPlane !== undefined &&
      targetPlane >= 0 &&
      sourcePlane === targetPlane
    ) {
      return sourcePlane;
    }

    if (sourcePlane !== undefined && sourcePlane >= 0) return sourcePlane;
    if (targetPlane !== undefined && targetPlane >= 0) return targetPlane;

    return 0;
  };

  const linkMarkup = links.map(link => {
    const a = position(link.source), b = position(link.target);
    const sourceType = nodeById.get(Number(link.source))?.type;
    const targetType = nodeById.get(Number(link.target))?.type;
    const isScaleUp = new Set([sourceType, targetType]).has('gpu')
      && new Set([sourceType, targetType]).has('nvswitch');
    const scope = isScaleUp ? 'scale-up-link' : 'scale-out-link';
    const plane = `plane-${inferLinkPlane(link)}`;
    const title = `${link.source}:${link.source_port ?? '—'} ↔ ${link.target}:${link.target_port ?? '—'} · ${link.bandwidth ?? ''} ${link.delay ?? ''}`;
    return `<line class="topology-link ${scope} ${plane}" data-source-node="${link.source}" data-target-node="${link.target}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"><title>${escapeHtml(title)}</title></line>`;
  }).join('');

  const tierLabels = [...scaleOutHierarchy.tiers.keys()]
    .sort((a, b) => b - a)
    .map(depth => {
      const tierNodes = scaleOutHierarchy.tiers.get(depth) || [];
      const yValues = tierNodes.map(node => position(node.id).y);
      const y = yValues.length ? yValues.reduce((sum, value) => sum + value, 0) / yValues.length : 0;
      const suffix = depth === 1
        ? 'ENDPOINT-FACING'
        : depth === scaleOutHierarchy.maxDepth
          ? 'CORE-SIDE'
          : 'INTERMEDIATE';
      return `<text x="42" y="${y - 34}" class="topology-layer-label">SCALE-OUT TIER ${depth} · ${suffix}</text>`;
    }).join('');
  const layerLabels = '';

  const nodeMarkup = nodes.map(node => {
    const p = position(node.id);
    const isGpu = node.type === 'gpu';
    const radius = isGpu ? 16 : node.type.includes('ocs') ? 23 : 21;
    const inside = isGpu ? node.id : String(node.id);
    return `<g class="topology-node" data-node-id="${node.id}" transform="translate(${p.x},${p.y})">
      <circle class="node-shape ${node.type}" r="${radius}"></circle>
      <text class="node-label inside" y="4">${escapeHtml(inside)}</text>
      <text class="node-label" y="${radius + 17}">${escapeHtml(node.label)}</text>
      <title>${escapeHtml(`${node.label} · degree ${node.degree}${scaleOutHierarchy.depth.has(Number(node.id)) ? ` · inferred scale-out tier ${scaleOutHierarchy.depth.get(Number(node.id))}` : ''}`)}</title>
    </g>`;
  }).join('');

  $('topologyCanvas').innerHTML = `<svg id="topologySvg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet">
    <rect width="${width}" height="${height}" fill="transparent"></rect>
    <g id="topologyViewport" transform="translate(${state.topology.tx},${state.topology.ty}) scale(${state.topology.scale})">
      ${domainMarkup}${layerLabels}${serverBoxes}${linkMarkup}${nodeMarkup}
    </g>
  </svg>`;
  bindTopologyInteractions();
}

function svgPoint(svg, event) {
  const point = svg.createSVGPoint();
  point.x = event.clientX; point.y = event.clientY;
  return point.matrixTransform(svg.getScreenCTM().inverse());
}

function bindTopologyInteractions() {
  const svg = $('topologySvg');
  const viewport = $('topologyViewport');
  if (!svg || !viewport) return;

  const applyViewportTransform = () => {
    viewport.setAttribute(
      'transform',
      `translate(${state.topology.tx},${state.topology.ty}) scale(${state.topology.scale})`
    );
  };

  const clientToSvg = (clientX, clientY) => {
    const point = svg.createSVGPoint();
    point.x = clientX;
    point.y = clientY;

    const ctm = svg.getScreenCTM();
    return ctm
      ? point.matrixTransform(ctm.inverse())
      : { x: clientX, y: clientY };
  };

  const updateDraggedNode = nodeId => {
    const position = state.topology.positions[nodeId];
    if (!position) return;

    const node = [...svg.querySelectorAll('.topology-node[data-node-id]')]
      .find(item => Number(item.dataset.nodeId) === nodeId);

    if (node) {
      node.setAttribute(
        'transform',
        `translate(${position.x},${position.y})`
      );
    }

    svg.querySelectorAll(
      '.topology-link[data-source-node][data-target-node]'
    ).forEach(line => {
      const sourceId = Number(line.dataset.sourceNode);
      const targetId = Number(line.dataset.targetNode);

      if (sourceId !== nodeId && targetId !== nodeId) return;

      const source = state.topology.positions[sourceId];
      const target = state.topology.positions[targetId];
      if (!source || !target) return;

      line.setAttribute('x1', source.x);
      line.setAttribute('y1', source.y);
      line.setAttribute('x2', target.x);
      line.setAttribute('y2', target.y);
    });
  };

  let activePointerId = null;
  let draggedNodeId = null;
  let draggedElement = null;
  let panPoint = null;
  let pendingDragPoint = null;
  let dragFrame = 0;

  const flushDrag = () => {
    dragFrame = 0;

    if (draggedNodeId === null || !pendingDragPoint) return;

    const pointer = clientToSvg(
      pendingDragPoint.clientX,
      pendingDragPoint.clientY
    );
    pendingDragPoint = null;

    state.topology.positions[draggedNodeId] = {
      x: (pointer.x - state.topology.tx) / state.topology.scale,
      y: (pointer.y - state.topology.ty) / state.topology.scale,
    };

    updateDraggedNode(draggedNodeId);
  };

  // Zoom without rebuilding the SVG.
  svg.addEventListener('wheel', event => {
    event.preventDefault();

    const cursor = clientToSvg(event.clientX, event.clientY);
    const oldScale = state.topology.scale;
    const factor = event.deltaY < 0 ? 1.1 : 0.9;
    const newScale = Math.max(
      0.35,
      Math.min(3.2, oldScale * factor)
    );

    if (newScale === oldScale) return;

    const contentX = (cursor.x - state.topology.tx) / oldScale;
    const contentY = (cursor.y - state.topology.ty) / oldScale;

    state.topology.scale = newScale;
    state.topology.tx = cursor.x - contentX * newScale;
    state.topology.ty = cursor.y - contentY * newScale;

    applyViewportTransform();
  }, { passive: false });

  svg.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;

    const node = event.target.closest?.('.topology-node');

    activePointerId = event.pointerId;
    svg.setPointerCapture(event.pointerId);
    event.preventDefault();

    if (node) {
      event.stopPropagation();

      draggedNodeId = Number(node.dataset.nodeId);
      draggedElement = node;
      state.topology.draggingNode = draggedNodeId;

      draggedElement.classList.add('is-dragging');
      pendingDragPoint = {
        clientX: event.clientX,
        clientY: event.clientY,
      };
      return;
    }

    state.topology.panning = true;
    panPoint = clientToSvg(event.clientX, event.clientY);
  });

  svg.addEventListener('pointermove', event => {
    if (event.pointerId !== activePointerId) return;

    if (draggedNodeId !== null) {
      pendingDragPoint = {
        clientX: event.clientX,
        clientY: event.clientY,
      };

      // Limit DOM updates to one per animation frame.
      if (!dragFrame) {
        dragFrame = requestAnimationFrame(flushDrag);
      }
      return;
    }

    if (!state.topology.panning || !panPoint) return;

    const current = clientToSvg(event.clientX, event.clientY);

    state.topology.tx += current.x - panPoint.x;
    state.topology.ty += current.y - panPoint.y;
    panPoint = current;

    applyViewportTransform();
  });

  const finishPointer = event => {
    if (
      activePointerId === null ||
      event.pointerId !== activePointerId
    ) {
      return;
    }

    if (dragFrame) {
      cancelAnimationFrame(dragFrame);
      flushDrag();
    }

    const shouldRefreshBounds = draggedNodeId !== null;

    draggedElement?.classList.remove('is-dragging');

    draggedNodeId = null;
    draggedElement = null;
    pendingDragPoint = null;
    panPoint = null;

    state.topology.draggingNode = null;
    state.topology.panning = false;

    if (svg.hasPointerCapture(event.pointerId)) {
      svg.releasePointerCapture(event.pointerId);
    }

    activePointerId = null;

    // Recalculate Server and Scale-up/Scale-out boxes after dropping.
    if (shouldRefreshBounds) {
      renderTopology();
    }
  };

  svg.addEventListener('pointerup', finishPointer);
  svg.addEventListener('pointercancel', finishPointer);
}

function initializeFlowFilters() {
  const sports = state.data.flows?.summary?.sports || [];
  $('sportFilter').innerHTML = '<option value="all">All sports</option>' + sports.map(sport => `<option value="${sport}">${sport}</option>`).join('');
  $('sportFilter').value = state.sportFilter = 'all';
  $('throughputNote').textContent = state.data.throughput?.note || '';
}

function filteredFlows(forTable = false) {
  let flows = [...(state.data.flows?.flows || [])];
  const query = (forTable ? state.tableFilter : state.flowFilter).toLowerCase();
  if (!forTable && state.sportFilter !== 'all') flows = flows.filter(flow => String(flow.sport) === state.sportFilter);
  if (query) flows = flows.filter(flow => `${flow.id} ${flow.src} ${flow.dst} ${flow.sport}`.toLowerCase().includes(query));
  if (!forTable && state.flowLimit !== 'all') {
    const limit = Number(state.flowLimit);
    flows.sort((a, b) => b.fct_us - a.fct_us);
    flows = flows.slice(0, limit);
  }
  return flows;
}

function renderFlows() {
  renderThroughputChart();
  renderFlowTable();
}

function renderThroughputChart() {
  const flows = filteredFlows(false);
  if (!flows.length) {
    $('throughputChart').innerHTML = emptyState('No flows match the current filters.');
    $('flowLegend').innerHTML = '';
    return;
  }

  const throughput = state.data.throughput || {};
  const actualSeries = new Map((throughput.series || []).map(item => [item.id, item]));
  const useActual = throughput.kind === 'flow_rx_log' && actualSeries.size > 0;
  const width = 1250, height = 390;
  const margin = { left: 68, right: 24, top: 24, bottom: 46 };

  let xMin;
  let xMaxRaw;
  let yMaxRaw;

  if (useActual) {
    const samples = flows.flatMap(flow => actualSeries.get(flow.id)?.samples || []);
    if (!samples.length) {
      $('throughputChart').innerHTML = emptyState('Selected flows have no [FLOW_RX_BYTES] samples.');
      $('flowLegend').innerHTML = '';
      return;
    }
    xMin = Math.min(...samples.map(item => item.start_us));
    xMaxRaw = Math.max(...samples.map(item => item.end_us));
    yMaxRaw = Math.max(...samples.map(item => item.gbps));
  } else {
    xMin = Math.min(...flows.map(f => f.start_us));
    xMaxRaw = Math.max(...flows.map(f => f.end_us));
    yMaxRaw = Math.max(...flows.map(f => f.avg_gbps));
  }

  const xMax = xMaxRaw <= xMin ? xMin + 1 : xMaxRaw;
  const yMax = yMaxRaw <= 0 ? 1 : yMaxRaw * 1.12;
  const sx = value => margin.left + (value - xMin) / (xMax - xMin) * (width - margin.left - margin.right);
  const sy = value => height - margin.bottom - value / yMax * (height - margin.top - margin.bottom);
  const baseline = height - margin.bottom;

  const xTicks = 6, yTicks = 5;
  const grid = [];
  for (let i = 0; i <= xTicks; i++) {
    const x = margin.left + i / xTicks * (width - margin.left - margin.right);
    const value = xMin + i / xTicks * (xMax - xMin);
    grid.push(`<line class="grid-line" x1="${x}" y1="${margin.top}" x2="${x}" y2="${baseline}"></line><text class="axis-label" x="${x}" y="${height - 18}" text-anchor="middle">${fmt(value / 1000, 2)}</text>`);
  }
  for (let i = 0; i <= yTicks; i++) {
    const y = baseline - i / yTicks * (height - margin.top - margin.bottom);
    const value = i / yTicks * yMax;
    grid.push(`<line class="grid-line" x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}"></line><text class="axis-label" x="${margin.left - 10}" y="${y + 4}" text-anchor="end">${fmt(value, 1)}</text>`);
  }

  const paths = flows.map((flow, index) => {
    const color = palette[index % palette.length];
    const isolated = state.isolatedFlow;
    const classes = isolated ? (isolated === flow.id ? 'selected' : 'dimmed') : '';

    if (useActual) {
      const series = actualSeries.get(flow.id);
      const samples = [...(series?.samples || [])].sort((a, b) => a.start_us - b.start_us);
      if (!samples.length) return '';

      // A per-flow throughput timeline is rendered as rectangular active intervals:
      // rise at interval start, remain flat at the interval-average throughput,
      // then fall at interval end. Consecutive buckets belong to one rectangle;
      // a real time gap starts a new rectangle.
      const intervals = [];
      let current = null;
      const epsilonUs = 1e-6;

      samples.forEach(sample => {
        const startUs = Number(sample.start_us);
        const endUs = Number(sample.end_us);
        const bytes = Number(sample.bytes || 0);
        if (!Number.isFinite(startUs) || !Number.isFinite(endUs) || endUs <= startUs) return;

        if (!current || startUs > current.end_us + epsilonUs) {
          if (current) intervals.push(current);
          current = {
            start_us: startUs,
            end_us: endUs,
            bytes,
          };
        } else {
          current.end_us = Math.max(current.end_us, endUs);
          current.bytes += bytes;
        }
      });
      if (current) intervals.push(current);

      const intervalMarkup = intervals.map(interval => {
        const durationNs = (interval.end_us - interval.start_us) * 1000.0;
        const gbps = durationNs > 0 ? interval.bytes * 8.0 / durationNs : 0;
        const x1 = sx(interval.start_us);
        const x2 = sx(interval.end_us);
        const y = sy(gbps);
        const d = `M ${x1} ${baseline} L ${x1} ${y} L ${x2} ${y} L ${x2} ${baseline}`;
        const title = `${flow.id} · ${fmt(gbps, 3)} Gbps · ${fmt(interval.start_us / 1000, 3)}–${fmt(interval.end_us / 1000, 3)} ms`;
        return `<path class="flow-path throughput-rect ${classes}" data-flow-id="${escapeHtml(flow.id)}" d="${d}" stroke="${color}" style="fill:${color};fill-opacity:.10"><title>${escapeHtml(title)}</title></path>`;
      }).join('');

      return intervalMarkup;
    }

    const x1 = sx(flow.start_us), x2 = sx(flow.end_us), y = sy(flow.avg_gbps);
    const d = `M ${x1} ${baseline} L ${x1} ${y} L ${x2} ${y} L ${x2} ${baseline}`;
    return `<path class="flow-path ${classes}" data-flow-id="${escapeHtml(flow.id)}" d="${d}" stroke="${color}"><title>${escapeHtml(flow.id)} · ${fmt(flow.avg_gbps, 3)} Gbps · FCT ${fmt(flow.fct_us, 3)} µs</title></path>`;
  }).join('');

  $('throughputChart').innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    ${grid.join('')}
    <line class="axis-line" x1="${margin.left}" y1="${baseline}" x2="${width - margin.right}" y2="${baseline}"></line>
    <line class="axis-line" x1="${margin.left}" y1="${margin.top}" x2="${margin.left}" y2="${baseline}"></line>
    <text class="axis-label" x="${width / 2}" y="${height - 3}" text-anchor="middle">Time (ms)</text>
    <text class="axis-label" transform="translate(13 ${height / 2}) rotate(-90)" text-anchor="middle">Throughput (Gbps)</text>
    ${paths}
  </svg>`;

  $('flowLegend').innerHTML = flows.map((flow, index) => `
    <button class="legend-item ${state.isolatedFlow === flow.id ? 'selected' : ''}" data-legend-flow="${escapeHtml(flow.id)}" title="Click to isolate; click again to restore all">
      <span class="legend-swatch" style="background:${palette[index % palette.length]}"></span>${escapeHtml(flow.id)}
    </button>
  `).join('');
  document.querySelectorAll('[data-legend-flow]').forEach(item => item.addEventListener('click', () => {
    state.isolatedFlow = state.isolatedFlow === item.dataset.legendFlow ? null : item.dataset.legendFlow;
    renderThroughputChart();
  }));
  document.querySelectorAll('.flow-path').forEach(item => item.addEventListener('click', () => {
    state.isolatedFlow = state.isolatedFlow === item.dataset.flowId ? null : item.dataset.flowId;
    renderThroughputChart();
  }));
}

function renderFlowTable() {
  let flows = filteredFlows(true);
  const { key, direction } = state.flowSort;
  flows.sort((a, b) => {
    const av = a[key] ?? -Infinity, bv = b[key] ?? -Infinity;
    if (typeof av === 'string') return direction === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
    return direction === 'asc' ? av - bv : bv - av;
  });
  $('flowCount').textContent = `${flows.length} / ${state.data.flows?.summary?.count || 0} flows`;
  $('flowTableBody').innerHTML = flows.map(flow => `
    <tr>
      <td>${escapeHtml(flow.src)}</td><td>${escapeHtml(flow.dst)}</td><td>${escapeHtml(flow.sport)}</td>
      <td>${escapeHtml(flow.size_human)}</td><td>${fmt(flow.start_us, 3)}</td><td>${fmt(flow.fct_us, 3)}</td>
      <td>${fmt(flow.avg_gbps, 3)}</td><td>${flow.slowdown === null ? '—' : fmt(flow.slowdown, 2)}</td>
    </tr>
  `).join('') || '<tr class="empty-row"><td colspan="8">No flows match the current filter.</td></tr>';
}

function renderSchedule() {
  const schedule = state.data.schedule || {};
  const configs = schedule.configs || [];
  $('ocsSelector').innerHTML = configs.map(item => `<option value="${item.ocs}">OCS ${item.ocs}</option>`).join('');
  if (!configs.length) {
    $('ocsScheduleSummary').innerHTML = '';
    $('ocsScheduleBody').innerHTML = '<tr class="empty-row"><td colspan="2">No OCS schedule file was found.</td></tr>';
    return;
  }
  renderSelectedSchedule();
}

function renderSelectedSchedule() {
  const selected = Number($('ocsSelector').value || state.data.schedule.configs[0].ocs);
  const config = state.data.schedule.configs.find(item => item.ocs === selected);
  const entries = state.data.schedule.entries?.[String(selected)] || {};
  $('ocsScheduleSummary').innerHTML = [
    ['Epoch', `${config.epoch_start_us} µs`],
    ['Slice', `${config.slice_duration_us} µs`],
    ['Switching', `${config.switching_time_us} µs`],
    ['Slices', config.num_slices],
  ].map(([label, value]) => `<span class="inline-stat">${label}: <strong>${escapeHtml(value)}</strong></span>`).join('');
  const rows = [];
  for (let slice = 0; slice < config.num_slices; slice++) {
    const pairs = entries[String(slice)] || [];
    rows.push(`<tr><td>${slice}</td><td>${pairs.length ? pairs.map(pair => `${pair[0]} ↔ ${pair[1]}`).join(' · ') : 'Idle'}</td></tr>`);
  }
  $('ocsScheduleBody').innerHTML = rows.join('');
}

function displayRnic(value) {
  const encodedPort = Number(value);

  if (!Number.isFinite(encodedPort) || encodedPort < 0) {
    return '—';
  }

  // Encoded format:
  // rnic_port = ((rnic_id + 1) << 16) | plane_id
  // 65536 and 65537 therefore both belong to RNIC 0.
  if (encodedPort >= 65536) {
    return (encodedPort >>> 16) - 1;
  }

  return encodedPort;
}

function renderInjection() {
  const injection = state.data.injection || {};
  const mode = Number(injection.mode || 0);
  const modeLabel = {0:'Default RDMA',1:'RNIC',2:'User-space'}[mode] || 'Unknown';
  const wr = injection.wr?.totals || {};
  const stats = [
    ['Mode', `${mode} · ${modeLabel}`],
  ];

  if (mode === 2) {
    stats.push(
      ['WR flows', wr.flows || 0],
      ['WR posts', wr.posts || 0],
      ['Posted bytes', wr.bytes_human || '—'],
    );
  }

  $('injectionSummary').innerHTML = stats
    .map(([label, value]) => `<span class="inline-stat">${label}: <strong>${escapeHtml(value)}</strong></span>`)
    .join('');

  const sourceRows = injection.exact && injection.rows?.length
    ? injection.rows
    : (injection.gate_tables || []);

  const nodes = [...new Set(sourceRows
    .map(row => Number(row.node))
    .filter(Number.isFinite))]
    .sort((a, b) => a - b);

  const selector = $('injectionNodeSelector');
  selector.innerHTML = nodes
    .map(node => `<option value="${node}">Node ${node}</option>`)
    .join('');
  selector.disabled = nodes.length === 0;

  if (!nodes.includes(Number(state.injectionNode))) {
    state.injectionNode = nodes[0] ?? null;
  }

  if (state.injectionNode !== null) {
    selector.value = String(state.injectionNode);
  }

  renderSelectedInjectionNode();
}

function renderSelectedInjectionNode() {
  const injection = state.data.injection || {};
  const selectedNode = Number(state.injectionNode);
  const exactRows = injection.exact && injection.rows?.length
    ? injection.rows
    : [];

  if (exactRows.length) {
    const columns = [
      {key: 'node', label: 'node'},
      {key: 'rnic_port', label: 'rnic', format: displayRnic},
      {key: 'plane', label: 'plane'},
      {key: 'start_ns', label: 'start_ns'},
      {key: 'end_ns', label: 'end_ns'},
      {key: 'period_ns', label: 'period_ns'},
      {key: 'destinations', label: 'destinations'},
    ];

    const rows = exactRows.filter(row => Number(row.node) === selectedNode);
    $('injectionHead').innerHTML = `<tr>${columns.map(column => `<th>${escapeHtml(column.label)}</th>`).join('')}</tr>`;
    $('injectionBody').innerHTML = rows.map(row => `
      <tr>${columns.map(column => {
        const value = column.format ? column.format(row[column.key]) : (row[column.key] ?? '—');
        return `<td>${escapeHtml(value)}</td>`;
      }).join('')}</tr>
    `).join('') || `<tr class="empty-row"><td colspan="${columns.length}">No Injection Windows for Node ${escapeHtml(selectedNode)}.</td></tr>`;
    return;
  }

  const columns = [
    {key: 'node', label: 'node'},
    {key: 'rnic_port', label: 'rnic', format: displayRnic},
    {key: 'periodNs', label: 'period_ns'},
    {key: 'slots', label: 'slots'},
  ];
  const rows = (injection.gate_tables || [])
    .filter(row => Number(row.node) === selectedNode);

  $('injectionHead').innerHTML = `<tr>${columns.map(column => `<th>${escapeHtml(column.label)}</th>`).join('')}</tr>`;
  $('injectionBody').innerHTML = rows.map(row => `
    <tr>${columns.map(column => {
      const value = column.format ? column.format(row[column.key]) : (row[column.key] ?? '—');
      return `<td>${escapeHtml(value)}</td>`;
    }).join('')}</tr>
  `).join('') || `<tr class="empty-row"><td colspan="${columns.length}">No gate-table telemetry for Node ${escapeHtml(selectedNode)}.</td></tr>`;
}

function renderOcsStats() {
  const rows = state.data.ocs_stats || [];
  $('ocsStatsBody').innerHTML = rows.map(row => `
    <tr>
      <td>${escapeHtml(row.node)}</td>
      <td>${fmt(row.forwarded_packets ?? row.fwd_pkts, 0)}</td>
      <td>${fmt(row.forwarded_bytes ?? row.fwd_bytes, 0)}</td>
      <td>${fmt(row.drop_switching ?? row.sw_drops, 0)}</td>
      <td>${fmt(row.drop_no_circuit ?? row.no_circ_drops, 0)}</td>
    </tr>
  `).join('') || '<tr class="empty-row"><td colspan="5">No OCS Stats data. Check OCS_STATS_ENABLE and simulator.log.</td></tr>';
}

function renderRetransmission() {
  const rawRows = state.data.retransmission || [];
  const groups = new Map();

  const numberValue = value => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  };

  const ensureRnic = row => {
    const node = Number(row.node);
    const rnicPort = Number(row.rnic_port);

    // Exclude local Scale-up/NVSwitch flows and unbound QPs.
    if (!Number.isFinite(node) ||
        !Number.isFinite(rnicPort) ||
        rnicPort < 0) {
      return null;
    }

    const key = `${node}:${rnicPort}`;

    if (!groups.has(key)) {
      const planeValue = Number(row.plane);

      groups.set(key, {
        node,
        rnic_port: rnicPort,
        plane: Number.isFinite(planeValue) && planeValue >= 0
          ? planeValue
          : null,
        completed_qps: 0,
        retrans_packets: 0,
        retrans_bytes: 0,
        nack_count: 0,
        timeout_count: 0,
      });
    }

    const group = groups.get(key);
    const planeValue = Number(row.plane);

    if (group.plane === null &&
        Number.isFinite(planeValue) &&
        planeValue >= 0) {
      group.plane = planeValue;
    }

    return group;
  };

  // Seed all installed RNICs, including RNICs whose counters are all zero.
  [
    ...(state.data.injection?.rows || []),
    ...(state.data.injection?.gate_tables || []),
  ].forEach(ensureRnic);

  // Aggregate per-QP telemetry into one row per RNIC.
  rawRows.forEach(row => {
    const group = ensureRnic(row);
    if (!group) return;

    group.completed_qps += 1;
    group.retrans_packets += numberValue(row.retrans_packets);
    group.retrans_bytes += numberValue(row.retrans_bytes);
    group.nack_count += numberValue(row.nack_count);
    group.timeout_count += numberValue(row.timeout_count);
  });

  const rows = [...groups.values()].sort((a, b) =>
    a.node - b.node ||
    a.rnic_port - b.rnic_port
  );

  const columns = [
    'node',
    'rnic',
    'plane',
    'Completed QPs',
    'Retrans Pkts',
    'Retrans Bytes',
    'NACKs',
    'Timeouts',
  ];

  $('retransHead').innerHTML =
    `<tr>${columns.map(column => `<th>${column}</th>`).join('')}</tr>`;

  if (!rows.length) {
    $('retransBody').innerHTML =
      '<tr class="empty-row"><td colspan="8">No bound Scale-out RNIC telemetry was recorded.</td></tr>';
    return;
  }

  $('retransBody').innerHTML = rows.map(row => `
    <tr>
      <td>${escapeHtml(row.node)}</td>
      <td>${escapeHtml(displayRnic(row.rnic_port))}</td>
      <td>${row.plane === null ? '—' : escapeHtml(row.plane)}</td>
      <td>${fmt(row.completed_qps, 0)}</td>
      <td>${fmt(row.retrans_packets, 0)}</td>
      <td>${fmt(row.retrans_bytes, 0)}</td>
      <td>${fmt(row.nack_count, 0)}</td>
      <td>${fmt(row.timeout_count, 0)}</td>
    </tr>
  `).join('');
}

function bindEvents() {
  $('experimentSearch').addEventListener('input', event => { state.experimentFilter = event.target.value; renderExperimentList(); });
  $('refreshExperiments').addEventListener('click', () => loadExperiments(state.selectedName));
  $('topologyReset').addEventListener('click', () => {
    state.topology = { scale: 1, tx: 0, ty: 0, draggingNode: null, panning: false, lastX: 0, lastY: 0, positions: {} };
    renderTopology();
  });
  $('sportFilter').addEventListener('change', event => { state.sportFilter = event.target.value; state.isolatedFlow = null; renderThroughputChart(); });
  $('flowSearch').addEventListener('input', event => { state.flowFilter = event.target.value; state.isolatedFlow = null; renderThroughputChart(); });
  $('flowLimit').addEventListener('change', event => { state.flowLimit = event.target.value; state.isolatedFlow = null; renderThroughputChart(); });
  $('flowShowAll').addEventListener('click', () => { state.isolatedFlow = null; renderThroughputChart(); });
  $('flowTableSearch').addEventListener('input', event => { state.tableFilter = event.target.value; renderFlowTable(); });
  document.querySelectorAll('th[data-sort]').forEach(th => th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (state.flowSort.key === key) state.flowSort.direction = state.flowSort.direction === 'asc' ? 'desc' : 'asc';
    else state.flowSort = { key, direction: 'asc' };
    renderFlowTable();
  }));
  $('ocsSelector').addEventListener('change', renderSelectedSchedule);
  $('injectionNodeSelector').addEventListener('change', event => {
    state.injectionNode = Number(event.target.value);
    renderSelectedInjectionNode();
  });
}

bindEvents();
loadExperiments();
