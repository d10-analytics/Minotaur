(function () {
  "use strict";
  var payload = JSON.parse(document.getElementById("minotaur-presentation").textContent);
  var graph = payload.graph || { nodes: [], relationships: [] };
  var comparisonPayload = payload.comparison || null;
  // A comparison payload is present even when it has no drawable node, for
  // example a comparison whose only stored change is an added empty system.
  var comparisonMode = Boolean(comparisonPayload);
  var revisionView = comparisonMode ? "combined" : null;
  // Excerpts are deliberately a separate presentation concern: graph JSON
  // remains portable structural evidence even when no source root is trusted.
  var excerptPaths = (payload.excerpts && payload.excerpts.paths) || {};
  var callSiteAssociations = (payload.excerpts && payload.excerpts.call_sites) || {};
  var systemNames = payload.systems || [];
  var nodeSystems = payload.node_systems || {};
  var byId = new Map(graph.nodes.map(function (n) { return [n.id, n]; }));
  var layoutDir = "TB";
  var activeLayout = null;
  var layoutRuns = 0;
  var comparisonLayoutCache = new Map();
  var comparisonContainerGeometry = new Map();
  // The emphasis control, the no-change message, and the detail panel are all
  // presentations of stored comparison facts. Nothing here recomputes status
  // from the two snapshots; the payload already classified every record.
  var emphasisEl = document.getElementById("emphasis-changes");
  var noChangeState = comparisonMode
    && comparisonPayload !== null
    && comparisonPayload.changed === false;
  var comparisonLimitations = comparisonMode && comparisonPayload
    ? (comparisonPayload.limitations || [])
    : [];
  // Change emphasis is derived only from stored statuses: a changed record, or a
  // call-change residual for the relationship, plus the endpoints of changed
  // relationships. It is never inferred from a label or a source difference.
  var changedNodeIds = new Set();
  var changedEdgeIds = new Set();
  var themeModeEl = document.getElementById("theme-mode");
  var systemColorScheme = window.matchMedia("(prefers-color-scheme: dark)");
  // One shared value prevents node and edge labels from drifting apart as the
  // graph style evolves; edge weight adds hierarchy without reducing legibility.
  var GRAPH_LABEL_FONT_SIZE = "14px";
  var SYSTEM_CONTAINER_COLORS = [
    "#2f6f9f", "#b85c2c", "#4f7f52", "#7a5aa6", "#9a4f68",
    "#287f8f", "#6b6fa8", "#a64b4b", "#3f7c70", "#76543f"
  ];

  function sidePresent(record, side) {
    return Boolean(record && record[side] !== null && record[side] !== undefined);
  }

  function sidePayload(record, side) {
    if (!comparisonMode) return record;
    var value = record && record[side];
    if (Array.isArray(value)) value = value[0] || null;
    if (value && typeof value === "object" && value.node && typeof value.node === "object") {
      // Comparison node sides store the canonical node together with the
      // declared system name on that revision.
      return value.node;
    }
    return value && typeof value === "object" ? value : null;
  }

  function sideSystemRecord(record, side) {
    var value = record && record[side];
    if (Array.isArray(value)) value = value[0] || null;
    if (value && typeof value === "object" && value.node && typeof value.node === "object") {
      return value;
    }
    return null;
  }

  function preferredPayload(record) {
    if (!comparisonMode) return record;
    var preferred = record && record.default_side === "before" ? "before" : "after";
    return sidePayload(record, preferred) || sidePayload(record, preferred === "after" ? "before" : "after") || {};
  }

  function recordPresence(record, view) {
    if (!comparisonMode) return true;
    if (view === "before") return sidePresent(record, "before");
    if (view === "after") return sidePresent(record, "after");
    return sidePresent(record, "before") || sidePresent(record, "after");
  }

  function titleCase(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/[_-]+/g, " ")
      .replace(/(^|\s)([a-z])/g, function (match, space, letter) {
        return space + letter.toUpperCase();
      });
  }

  function recordChanged(record) {
    return Boolean(record) && ["added", "removed", "changed"].indexOf(record.status) >= 0;
  }

  function comparisonCallChanges() {
    return comparisonMode && comparisonPayload && Array.isArray(comparisonPayload.calls)
      ? comparisonPayload.calls
      : [];
  }

  function callChangeFor(relationshipId) {
    var found = null;
    comparisonCallChanges().forEach(function (change) {
      if (change && (change.id === relationshipId
          || change.relationship_id === relationshipId)) {
        found = change;
      }
    });
    return found;
  }

  function computeChangeEmphasis() {
    changedNodeIds = new Set();
    changedEdgeIds = new Set();
    if (!comparisonMode) return;
    var callChanged = new Set();
    comparisonCallChanges().forEach(function (change) {
      if (recordChanged(change)) callChanged.add(change.id || change.relationship_id);
    });
    graph.nodes.forEach(function (node) {
      if (recordChanged(node)) changedNodeIds.add(node.id);
    });
    graph.relationships.forEach(function (relationship) {
      if (recordChanged(relationship) || callChanged.has(relationship.id)) {
        changedEdgeIds.add(relationship.id);
      }
    });
    graph.relationships.forEach(function (relationship) {
      if (!changedEdgeIds.has(relationship.id)) return;
      if (relationship.source) changedNodeIds.add(relationship.source);
      if (relationship.target) changedNodeIds.add(relationship.target);
    });
  }

  function comparisonExcerpts(side) {
    var excerpts = payload.excerpts || {};
    var sideRecord = excerpts[side];
    if (sideRecord && typeof sideRecord === "object") return sideRecord;
    return { paths: {}, call_sites: {} };
  }

  function excerptPathsForSide(side) {
    if (!comparisonMode) return excerptPaths;
    return comparisonExcerpts(side).paths || {};
  }

  function comparisonOrigin(side) {
    var revisions = comparisonPayload && comparisonPayload.revisions
      ? comparisonPayload.revisions
      : {};
    var name = side === "before" ? revisions.old : revisions.new;
    var label = side === "before" ? "Before" : "After";
    return "Captured " + label + " revision" + (name ? ": " + String(name) : "") + ".";
  }

  function sideLabel(side) {
    return side === "before" ? "Before" : "After";
  }

  function sideSystems(record, side) {
    if (!record) return [];
    if (comparisonMode && !sidePresent(record, side)) return [];
    if (comparisonMode) {
      // Node sides carry the declared system for that revision only. There is
      // deliberately no fallback to a cross-revision union: same-side internal
      // eligibility (V-21) must never combine both revisions' memberships.
      var stored = sideSystemRecord(record, side);
      if (stored && typeof stored.system === "string" && stored.system) return [stored.system];
      var sideMapped = payload.node_systems && payload.node_systems[record.id];
      if (sideMapped && typeof sideMapped === "object" && !Array.isArray(sideMapped)) {
        var mappedValue = sideMapped[side];
        if (typeof mappedValue === "string" && mappedValue) return [mappedValue];
      }
      return [];
    }
    var payloadValue = sidePayload(record, side);
    var candidates = [];
    if (payloadValue) {
      candidates = payloadValue.systems || payloadValue.involved_systems || payloadValue.system || [];
    }
    if (typeof candidates === "string") return candidates ? [candidates] : [];
    if (Array.isArray(candidates) && candidates.length) {
      return candidates.filter(function (name) { return typeof name === "string" && name; });
    }
    var mapped = payload.node_systems && payload.node_systems[record.id];
    if (mapped && typeof mapped === "object" && !Array.isArray(mapped)) {
      var mappedSide = mapped[side];
      if (typeof mappedSide === "string") return [mappedSide];
      if (Array.isArray(mappedSide) && mappedSide.length) return mappedSide;
    }
    if (Array.isArray(mapped)) return mapped;
    if (typeof mapped === "string") return [mapped];
    return Array.isArray(record.involved_systems) ? record.involved_systems : [];
  }

  function sideEndpointSystems(record, side, endpoint) {
    if (!record || !record.eligibility) return [];
    var value = record.eligibility[side];
    if (!value || typeof value !== "object") return [];
    var names = value[endpoint + "_systems"];
    return Array.isArray(names) ? names : [];
  }

  function membershipLabel(record, side) {
    if (!sidePresent(record, side)) return "Not present";
    var names = sideSystems(record, side);
    return names.length ? names.join(", ") : "unassigned";
  }

  function nodeBelongsTo(node, system, side) {
    var record = node.data("comparison_record");
    return sideSystems(record, side).indexOf(system) >= 0;
  }

  function nodeBelongsInView(node, system, view) {
    if (system === "") return true;
    if (!comparisonMode) return node.data("system") === system;
    if (view === "combined") {
      return nodeBelongsTo(node, system, "before") || nodeBelongsTo(node, system, "after");
    }
    return nodeBelongsTo(node, system, view);
  }

  function edgePresentInView(edge, view) {
    if (!comparisonMode) return true;
    var record = edge.data("comparison_record");
    return view === "combined"
      ? sidePresent(record, "before") || sidePresent(record, "after")
      : sidePresent(record, view);
  }

  function edgeHasInternalSide(edge, system, side) {
    // The stored relationship eligibility already resolves each side's endpoint
    // memberships, so this never compares memberships across revisions.
    var record = edge.data("comparison_record");
    return edgePresentInView(edge, side)
      && sideEndpointSystems(record, side, "source").indexOf(system) >= 0
      && sideEndpointSystems(record, side, "target").indexOf(system) >= 0;
  }

  function edgeHasInternalInView(edge, system, view) {
    if (!comparisonMode) return edge.source().data("system") === system
      && edge.target().data("system") === system;
    if (view === "combined") {
      return edgeHasInternalSide(edge, system, "before")
        || edgeHasInternalSide(edge, system, "after");
    }
    return edgeHasInternalSide(edge, system, view);
  }

  function edgeTouchesSide(edge, system, side) {
    var record = edge.data("comparison_record");
    return sideEndpointSystems(record, side, "source").indexOf(system) >= 0
      || sideEndpointSystems(record, side, "target").indexOf(system) >= 0;
  }

  function edgeTouchesSystemInView(edge, system, view) {
    if (!comparisonMode) {
      return edge.source().data("system") === system || edge.target().data("system") === system;
    }
    if (view === "combined") {
      return edgeTouchesSide(edge, system, "before") || edgeTouchesSide(edge, system, "after");
    }
    return edgeTouchesSide(edge, system, view);
  }

  function comparisonNodeData(record) {
    var value = preferredPayload(record);
    var systems = Array.isArray(record.involved_systems) ? record.involved_systems : [];
    return {
      id: record.id,
      label: value.label || record.id,
      node_class: value.node_class || "symbol",
      system: systems[0] || "",
      systems: systems,
      symbol_kind: value.symbol_kind || "",
      path: value.path || (value.location ? value.location.path : ""),
      reference_text: value.reference_text || "",
      location: value.location || null,
      status: record.status || "unchanged",
      comparison_record: record,
      comparison_before: record.before,
      comparison_after: record.after,
      bg: nodeColors(value.node_class || "symbol").bg,
      border: nodeColors(value.node_class || "symbol").border,
      label_color: activeTheme.text
    };
  }

  // Themes supply semantic roles, not a raw stylesheet swap: canvas-rendered
  // Cytoscape elements need the same palette as DOM controls. Yellow is absent
  // from every graph palette, and red remains reserved for selected edges.
  var THEMES = {
    "light": {
      accent: "#4a7c59", selected: "#c62828", text: "#1a1a1a",
      nodeClasses: {
        "file": { bg: "#d5e8d4", border: "#82b366" },
        "symbol": { bg: "#dae8fc", border: "#6c8ebf" },
        "unresolved-reference": { bg: "#eadcf2", border: "#8e5aa8" }
      },
      edgeKinds: {
        "contains": { bg: "#eeeeee", border: "#999999" },
        "calls": { bg: "#dae8fc", border: "#6c8ebf" },
        "references": { bg: "#f6dfcf", border: "#b85c2c" },
        "imports": { bg: "#d5e8d4", border: "#82b366" },
        "inherits": { bg: "#e1d5e7", border: "#9673a6" },
        "implements": { bg: "#d4e8e2", border: "#5a9a82" }
      }
    },
    "catppuccin-mocha": {
      accent: "#89b4fa", selected: "#f38ba8", text: "#cdd6f4",
      nodeClasses: {
        "file": { bg: "#253b32", border: "#a6e3a1" },
        "symbol": { bg: "#26344f", border: "#89b4fa" },
        "unresolved-reference": { bg: "#392f4b", border: "#cba6f7" }
      },
      edgeKinds: {
        "contains": { bg: "#45475a", border: "#bac2de" },
        "calls": { bg: "#26344f", border: "#89b4fa" },
        "references": { bg: "#44352e", border: "#fab387" },
        "imports": { bg: "#253b32", border: "#a6e3a1" },
        "inherits": { bg: "#392f4b", border: "#cba6f7" },
        "implements": { bg: "#28413e", border: "#94e2d5" }
      }
    },
    "nord-polar-night": {
      accent: "#88c0d0", selected: "#bf616a", text: "#eceff4",
      nodeClasses: {
        "file": { bg: "#35433e", border: "#a3be8c" },
        "symbol": { bg: "#334554", border: "#81a1c1" },
        "unresolved-reference": { bg: "#423b52", border: "#b48ead" }
      },
      edgeKinds: {
        "contains": { bg: "#434c5e", border: "#d8dee9" },
        "calls": { bg: "#334554", border: "#81a1c1" },
        "references": { bg: "#4c3b32", border: "#d08770" },
        "imports": { bg: "#35433e", border: "#a3be8c" },
        "inherits": { bg: "#423b52", border: "#b48ead" },
        "implements": { bg: "#30484b", border: "#8fbcbb" }
      }
    },
    "solarized-dark": {
      accent: "#2aa198", selected: "#dc322f", text: "#fdf6e3",
      nodeClasses: {
        "file": { bg: "#183d38", border: "#859900" },
        "symbol": { bg: "#123e4d", border: "#268bd2" },
        "unresolved-reference": { bg: "#3a3148", border: "#6c71c4" }
      },
      edgeKinds: {
        "contains": { bg: "#073642", border: "#93a1a1" },
        "calls": { bg: "#123e4d", border: "#268bd2" },
        "references": { bg: "#4a3025", border: "#cb4b16" },
        "imports": { bg: "#183d38", border: "#859900" },
        "inherits": { bg: "#3a3148", border: "#6c71c4" },
        "implements": { bg: "#123e4d", border: "#2aa198" }
      }
    }
  };

  function currentThemeName() {
    return themeModeEl.value === "system" && systemColorScheme.matches
      ? "catppuccin-mocha" : themeModeEl.value === "system" ? "light" : themeModeEl.value;
  }

  var activeThemeName = currentThemeName();
  var activeTheme = THEMES[activeThemeName];
  var CLASS_COLORS = activeTheme.nodeClasses;
  var EDGE_KIND_COLORS = activeTheme.edgeKinds;

  function nodeColors(nodeClass) {
    return CLASS_COLORS[nodeClass] || { bg: "#666666", border: "#aaaaaa" };
  }

  function edgeColors(kind) {
    return EDGE_KIND_COLORS[kind] || { bg: "#666666", border: "#aaaaaa" };
  }

  var elements = [];
  graph.nodes.forEach(function (node) {
    if (comparisonMode) {
      elements.push({ group: "nodes", data: comparisonNodeData(node) });
      return;
    }
    elements.push({ group: "nodes", data: {
      id: node.id, label: node.label, node_class: node.node_class,
      system: nodeSystems[node.id] || "",
      symbol_kind: node.symbol_kind || "", path: node.path || (node.location ? node.location.path : ""),
      reference_text: node.reference_text || "", location: node.location || null,
      bg: nodeColors(node.node_class).bg, border: nodeColors(node.node_class).border,
      label_color: activeTheme.text
    }});
  });
  graph.relationships.forEach(function (rel, i) {
    // The compact edge style has one provenance label, but the full evidence
    // array remains on the element so inspection never loses additional facts.
    var displayRelationship = preferredPayload(rel);
    var evidence = displayRelationship.evidence || [];
    var provenance = evidence.length > 0 ? evidence[0].provenance : "unknown";
    var colors = edgeColors(rel.kind || displayRelationship.kind);
    elements.push({ group: "edges", data: {
      id: comparisonMode ? rel.id : "edge-" + i,
      source: rel.source, target: rel.target,
      kind: rel.kind || displayRelationship.kind, provenance: provenance, evidence: evidence,
      status: rel.status || "unchanged", comparison_record: rel,
      comparison_before: rel.before, comparison_after: rel.after,
      // The extractor keys associations by canonical relationship index. Copy
      // them onto the interactive edge so later rendering never has to infer
      // call-site ownership from potentially duplicate evidence locations.
      call_sites: callSiteAssociations[String(i)] || [], edge_color: colors.border
    }});
  });

  function graphStyle(theme) {
    return [
      { selector: "node", style: {
        "label": "data(label)", "text-wrap": "ellipsis", "text-max-width": "180px",
        "font-size": GRAPH_LABEL_FONT_SIZE, "font-family": "system-ui, -apple-system, sans-serif",
        "text-valign": "center", "text-halign": "center",
        "width": "label", "height": "32px", "padding": "8px",
        "shape": "roundrectangle", "border-width": 2,
        "background-color": "data(bg)", "border-color": "data(border)", "color": "data(label_color)",
        "text-outline-color": "data(bg)", "text-outline-width": 0,
        "min-zoomed-font-size": 6
      }},
      { selector: "node.dimmed", style: { "opacity": 0.2 } },
      { selector: "node.faded", style: { "opacity": 0.5 } },
      { selector: "node.revision-hidden", style: { "opacity": 0 } },
      { selector: "edge.faded", style: { "opacity": 0.5 } },
      { selector: "node.highlighted", style: {
        "border-width": 6, "border-color": theme.accent, "opacity": 1, "z-index": 10
      }},
      { selector: "node:selected", style: { "border-width": 3, "border-color": theme.accent } },
      { selector: "node.outside-system", style: {
        "border-width": 6, "border-color": "#c62828"
      }},
      { selector: "node.system-container", style: {
        "label": "data(label)", "shape": "roundrectangle",
        "background-color": "data(container_color)", "background-opacity": 0.08,
        "border-color": "data(container_color)", "border-width": 4,
        "padding": "32px", "text-valign": "top", "text-halign": "center",
        "font-size": "18px", "font-weight": "bold", "color": "data(container_color)",
        "text-outline-width": 0, "compound-sizing-wrt-labels": "include"
      }},
      { selector: "node.selected-system-container", style: { "border-width": 7 } },
      { selector: "edge", style: {
        "width": 1.5, "line-color": "data(edge_color)", "target-arrow-color": "data(edge_color)",
        "target-arrow-shape": "triangle", "arrow-scale": 0.8, "curve-style": "bezier",
        "label": "data(kind)", "font-size": GRAPH_LABEL_FONT_SIZE, "font-weight": "bold",
        "font-family": "system-ui, -apple-system, sans-serif", "color": "data(edge_color)",
        "text-rotation": "autorotate", "text-margin-y": -8, "min-zoomed-font-size": 8
      }},
      { selector: "edge.cross-system", style: {
        "width": 4, "line-color": "#c62828", "target-arrow-color": "#c62828",
        "color": "#c62828", "z-index": 9
      }},
      { selector: "edge.highlighted", style: {
        "width": 3, "line-color": theme.selected, "target-arrow-color": theme.selected,
        "color": theme.selected, "z-index": 10
      }},
      { selector: "edge.dimmed", style: { "opacity": 0.12 } }
    ];
  }

  cytoscape.use(cytoscapeDagre);
  var cy = cytoscape({
    container: document.getElementById("cy"),
    elements: elements,
    style: graphStyle(activeTheme),
    layout: { name: "preset", fit: false },
    wheelSensitivity: 1,
    minZoom: 0.1,
    maxZoom: 4
  });
  if (comparisonMode) {
    var rawNodes = cy.nodes.bind(cy);
    cy.nodes = function (selector) {
      var collection = rawNodes(selector);
      return selector === ":visible"
        ? collection.not(".system-container, .revision-hidden")
        : collection;
    };
  }

  // --- System focus ---
  // Membership comes from committed exact-file definitions embedded by the
  // renderer. An individual selection retains its complete system plus only
  // the one-hop outside nodes joined to it by an enabled relationship.
  var systemFilterEl = document.getElementById("system-filter");
  var crossSystemConnectionsEl = document.getElementById("cross-system-connections");
  var revisionControlEl = document.getElementById("revision-control");
  var revisionViewEl = document.getElementById("revision-view");
  if (comparisonMode) {
    revisionControlEl.hidden = false;
    revisionViewEl.value = revisionView;
    revisionViewEl.addEventListener("change", function () {
      revisionView = revisionViewEl.value;
      // Revision changes are visibility changes over the already-laid-out
      // union. They must never move survivors, refit the camera, or rebuild a
      // filtered layout merely because a side was selected.
      applyFilters(false, { layout: false, preserveContainers: true });
    });
  }
  systemNames.forEach(function (name) {
    var option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    systemFilterEl.appendChild(option);
  });
  // C-08: the saved comparison may name a CLI-selected system. Initialize the
  // filter to it while leaving All Systems selectable.
  if (comparisonMode && comparisonPayload
      && typeof comparisonPayload.selected_system === "string"
      && systemNames.indexOf(comparisonPayload.selected_system) >= 0) {
    systemFilterEl.value = comparisonPayload.selected_system;
  }
  crossSystemConnectionsEl.disabled = systemFilterEl.value === "";
  systemFilterEl.addEventListener("change", function () {
    crossSystemConnectionsEl.disabled = systemFilterEl.value === "";
    applyFilters();
  });
  crossSystemConnectionsEl.addEventListener("change", applyFilters);

  function isCrossSystem(edge, selectedSystem) {
    if (comparisonMode && selectedSystem !== "") {
      var view = revisionView;
      if (view === "combined") {
        return !edgeHasInternalSide(edge, selectedSystem, "before")
          && !edgeHasInternalSide(edge, selectedSystem, "after")
          && edgeTouchesSystemInView(edge, selectedSystem, view);
      }
      return !edgeHasInternalSide(edge, selectedSystem, view)
        && edgeTouchesSystemInView(edge, selectedSystem, view);
    }
    var sourceSystem = edge.source().data("system");
    var targetSystem = edge.target().data("system");
    if (selectedSystem !== "") {
      return (sourceSystem === selectedSystem) !== (targetSystem === selectedSystem);
    }
    return sourceSystem !== "" && targetSystem !== "" && sourceSystem !== targetSystem;
  }

  function systemContainerColor(name) {
    var index = systemNames.indexOf(name);
    if (index < 0) index = systemNames.length;
    return SYSTEM_CONTAINER_COLORS[index % SYSTEM_CONTAINER_COLORS.length];
  }

  function clearSystemContainers() {
    var containers = cy.nodes(".system-container");
    if (!containers.length) return;
    containers.children().move({ parent: null });
    containers.remove();
  }

  function createSystemContainers(selectedSystem) {
    var groups = new Map();
    var containerNodes = comparisonMode
      ? checkedComparisonNodes().filter(function (node) {
        return comparisonLayoutEligible(node, selectedSystem, true);
      })
      : cy.nodes(":visible").not(".system-container");
    containerNodes.forEach(function (node) {
      var name = node.data("system") || "External / Unassigned";
      if (comparisonMode) {
        // V-08 placement: a node that survives into After is placed in its
        // After system; a removed node keeps its Before system. Membership is
        // read per side, never from the cross-revision union.
        var record = node.data("comparison_record");
        var placement = sideSystems(record, "after");
        if (!placement.length) placement = sideSystems(record, "before");
        name = placement[0] || "External / Unassigned";
      }
      if (!groups.has(name)) groups.set(name, cy.collection());
      groups.set(name, groups.get(name).union(node));
    });
    Array.from(groups.keys()).sort().forEach(function (name) {
      var id = "system-container:" + name;
      cy.add({
        group: "nodes",
        data: {
          id: id, label: name, node_class: "system-container", system: name,
          container_color: systemContainerColor(name)
        },
        classes: "system-container "
          + (name === selectedSystem ? "selected-system-container" : "boundary-system-container")
      });
      if (!comparisonMode) {
        groups.get(name).move({ parent: id });
      } else {
        var memberBox = groups.get(name).boundingBox({ includeLabels: true });
        var container = cy.getElementById(id);
        container.style({ width: memberBox.w + 64, height: memberBox.h + 64, padding: 0 });
        container.position({
          x: (memberBox.x1 + memberBox.x2) / 2,
          y: (memberBox.y1 + memberBox.y2) / 2,
        });
        container.lock();
      }
    });
    if (comparisonMode) {
      // Compound bounds normally follow only visible children. Freeze each
      // union container's geometry so a side switch can hide fact nodes while
      // retaining the same grouping context and viewport.
      cy.nodes(".system-container").forEach(function (container) {
        var geometry = comparisonContainerGeometry.get(container.id());
        if (geometry) {
          container.style({
            width: geometry.width,
            height: geometry.height,
            "min-width": geometry.width,
            "min-height": geometry.height,
            padding: 0,
          });
          container.position(geometry.position);
          container.lock();
        } else {
          var box = container.boundingBox({ includeLabels: true });
          comparisonContainerGeometry.set(container.id(), {
            width: box.w,
            height: box.h,
            position: container.position(),
          });
          container.style({
            width: box.w,
            height: box.h,
            "min-width": box.w,
            "min-height": box.h,
            padding: 0,
          });
          container.lock();
        }
      });
    }
  }

  // --- Filters: node classes ---
  // Build controls from the payload rather than a fixed taxonomy, so a valid
  // future graph class stays filterable even before a dedicated visual style is
  // added.
  var kindsEl = document.getElementById("kind-filters");
  var kinds = [];
  cy.nodes().forEach(function (n) {
    var k = n.data("node_class");
    if (kinds.indexOf(k) === -1) kinds.push(k);
  });
  kinds.sort();
  kinds.forEach(function (kind) {
    var c = nodeColors(kind);
    var lbl = document.createElement("label");
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = kind !== "unresolved-reference";
    cb.dataset.kind = kind;
    cb.style.setProperty("--kind-bg", c.bg);
    cb.style.setProperty("--kind-border", c.border);
    cb.addEventListener("change", applyFilters);
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" " + kind));
    kindsEl.appendChild(lbl);
  });

  // --- Filters: edge kinds ---
  var edgesEl = document.getElementById("edge-filters");
  var edgeKinds = [];
  cy.edges().forEach(function (e) {
    var k = e.data("kind");
    if (edgeKinds.indexOf(k) === -1) edgeKinds.push(k);
  });
  edgeKinds.sort();
  edgeKinds.forEach(function (kind) {
    var c = edgeColors(kind);
    var lbl = document.createElement("label");
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;
    cb.dataset.edgekind = kind;
    cb.style.setProperty("--kind-bg", c.bg);
    cb.style.setProperty("--kind-border", c.border);
    cb.addEventListener("change", applyFilters);
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" " + kind));
    edgesEl.appendChild(lbl);
  });

  function refreshFilterSwatches() {
    kindsEl.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      var colors = nodeColors(cb.dataset.kind);
      cb.style.setProperty("--kind-bg", colors.bg);
      cb.style.setProperty("--kind-border", colors.border);
    });
    edgesEl.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      var colors = edgeColors(cb.dataset.edgekind);
      cb.style.setProperty("--kind-bg", colors.bg);
      cb.style.setProperty("--kind-border", colors.border);
    });
  }

  function applyTheme() {
    activeThemeName = currentThemeName();
    activeTheme = THEMES[activeThemeName];
    CLASS_COLORS = activeTheme.nodeClasses;
    EDGE_KIND_COLORS = activeTheme.edgeKinds;
    if (themeModeEl.value === "system") {
      delete document.documentElement.dataset.theme;
    } else {
      document.documentElement.dataset.theme = activeThemeName;
    }
    cy.batch(function () {
      cy.nodes().forEach(function (node) {
        var colors = nodeColors(node.data("node_class"));
        node.data({ bg: colors.bg, border: colors.border, label_color: activeTheme.text });
      });
      cy.edges().forEach(function (edge) {
        edge.data("edge_color", edgeColors(edge.data("kind")).border);
      });
    });
    cy.style(graphStyle(activeTheme)).update();
    refreshFilterSwatches();
  }

  // A selected mode lives only in this open document. System mode listens for
  // an OS appearance change, but no setting is written into the downloaded file
  // or browser storage.
  themeModeEl.addEventListener("change", applyTheme);
  systemColorScheme.addEventListener("change", function () {
    if (themeModeEl.value === "system") applyTheme();
  });

  function changeEmphasisActive() {
    return comparisonMode && emphasisEl && !emphasisEl.disabled && emphasisEl.checked;
  }

  function searchQuery() {
    return searchEl ? searchEl.value.trim().toLowerCase() : "";
  }

  function nodeMatchesSearch(node, query) {
    var label = (node.data("label") || "").toLowerCase();
    var path = (node.data("path") || "").toLowerCase();
    var ref = (node.data("reference_text") || "").toLowerCase();
    return label.indexOf(query) >= 0 || path.indexOf(query) >= 0 || ref.indexOf(query) >= 0;
  }

  function applySelectionClasses(selection) {
    cy.elements().addClass("faded").removeClass("highlighted");
    if (selection.isNode()) {
      selection.addClass("highlighted").removeClass("faded");
      selection.neighborhood().addClass("highlighted").removeClass("faded");
    } else {
      selection.addClass("highlighted").removeClass("faded");
      selection.source().addClass("highlighted").removeClass("faded");
      selection.target().addClass("highlighted").removeClass("faded");
    }
  }

  function applySearchClasses(query) {
    // Search must not reveal an item the active view or filters exclude, so a
    // revision-hidden fact is skipped rather than highlighted back into view.
    cy.nodes().not(".system-container").forEach(function (node) {
      if (node.hidden() || node.hasClass("revision-hidden")) return;
      if (nodeMatchesSearch(node, query)) {
        node.removeClass("dimmed").addClass("highlighted");
      } else {
        node.addClass("dimmed").removeClass("highlighted");
      }
    });
    cy.edges().forEach(function (edge) {
      if (edge.hidden()) return;
      if (edge.source().hasClass("dimmed") && edge.target().hasClass("dimmed")) {
        edge.addClass("dimmed");
      } else {
        edge.removeClass("dimmed");
      }
    });
  }

  function applyChangeClasses() {
    cy.nodes().not(".system-container").forEach(function (node) {
      if (node.hidden() || node.hasClass("revision-hidden")) return;
      node.toggleClass("dimmed", !changedNodeIds.has(node.id()));
    });
    cy.edges().forEach(function (edge) {
      if (edge.hidden()) return;
      edge.toggleClass("dimmed", !changedEdgeIds.has(edge.id()));
    });
  }

  // One priority path: selection, then a nonempty search, then change emphasis,
  // then ordinary opacity. Each pass recomputes from the current state, so
  // clearing a lower-priority state can never override an active higher one.
  function applyEmphasis() {
    cy.batch(function () {
      cy.elements().removeClass("dimmed highlighted faded");
      if (selectedElement && selectedElement.visible()
          && !selectedElement.hasClass("revision-hidden")) {
        applySelectionClasses(selectedElement);
        return;
      }
      var query = searchQuery();
      if (query) {
        applySearchClasses(query);
        return;
      }
      if (changeEmphasisActive()) applyChangeClasses();
    });
  }

  function applyFilters(animate, options) {
    options = options || {};
    var shouldRelayout = options.layout !== false;
    if (!comparisonMode || shouldRelayout) {
      clearSystemContainers();
      if (comparisonMode) comparisonContainerGeometry.clear();
    }
    var hiddenKinds = [];
    kindsEl.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      if (!cb.checked) hiddenKinds.push(cb.dataset.kind);
    });
    var hiddenEdgeKinds = [];
    edgesEl.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      if (!cb.checked) hiddenEdgeKinds.push(cb.dataset.edgekind);
    });
    var selectedSystem = systemFilterEl.value;
    var showCrossSystemConnections = selectedSystem !== "" && crossSystemConnectionsEl.checked;
    var eligibleNodes = cy.nodes().not(".system-container").filter(function (node) {
      return hiddenKinds.indexOf(node.data("node_class")) < 0
        && (!comparisonMode || recordPresence(node.data("comparison_record"), "combined"));
    });
    var visibleNodeIds = new Set();
    var currentView = comparisonMode ? revisionView : "combined";
    if (selectedSystem === "") {
      eligibleNodes.forEach(function (node) {
        if (recordPresence(node.data("comparison_record"), currentView)) visibleNodeIds.add(node.id());
      });
    } else {
      var selectedNodes = eligibleNodes.filter(function (node) {
        return nodeBelongsInView(node, selectedSystem, comparisonMode ? revisionView : "combined");
      });
      selectedNodes.forEach(function (node) { visibleNodeIds.add(node.id()); });
      if (showCrossSystemConnections) {
        cy.edges().forEach(function (edge) {
          if (hiddenEdgeKinds.indexOf(edge.data("kind")) >= 0) return;
          var source = edge.source();
          var target = edge.target();
          if (!edgePresentInView(edge, currentView)) return;
          var sourceSelected = nodeBelongsInView(source, selectedSystem, currentView);
          var targetSelected = nodeBelongsInView(target, selectedSystem, currentView);
          if (sourceSelected && eligibleNodes.contains(target)
              && (!options.preserveContainers || nodeBelongsInView(
                target, selectedSystem, currentView
              ))
              && edgeTouchesSystemInView(edge, selectedSystem, currentView)) visibleNodeIds.add(target.id());
          if (targetSelected && eligibleNodes.contains(source)
              && (!options.preserveContainers || nodeBelongsInView(
                source, selectedSystem, currentView
              ))
              && edgeTouchesSystemInView(edge, selectedSystem, currentView)) visibleNodeIds.add(source.id());
        });
      }
    }
    cy.batch(function () {
      cy.nodes().forEach(function (node) {
        if (options.preserveContainers && node.hasClass("system-container")) {
          node.show();
        } else if (visibleNodeIds.has(node.id())) {
          if (options.preserveContainers && comparisonMode) {
            if (node.hidden()) node.show();
          } else {
            node.show();
          }
          node.removeClass("revision-hidden");
        } else if (options.preserveContainers && comparisonMode) {
          // Keep revision-excluded facts in the compound bounds while making
          // them unavailable to the visible selector and renderer.
          if (node.hidden()) node.show();
          node.addClass("revision-hidden");
        } else {
          node.hide();
          node.removeClass("revision-hidden");
        }
        if (!options.preserveContainers) {
          node.toggleClass(
            "outside-system",
            showCrossSystemConnections && !nodeBelongsInView(
              node, selectedSystem, comparisonMode ? revisionView : "combined"
            )
          );
        }
      });
      cy.edges().forEach(function (e) {
        var srcHidden = comparisonMode
          ? e.source().hasClass("revision-hidden") : !e.source().visible();
        var tgtHidden = comparisonMode
          ? e.target().hasClass("revision-hidden") : !e.target().visible();
        var unrelated = selectedSystem !== "" && !edgeTouchesSystemInView(e, selectedSystem, currentView);
        var wrongSide = comparisonMode && !edgePresentInView(e, currentView);
        var invalidInternal = selectedSystem !== "" && !showCrossSystemConnections
          && !edgeHasInternalInView(e, selectedSystem, currentView);
        if (srcHidden || tgtHidden || unrelated || wrongSide || invalidInternal
            || hiddenEdgeKinds.indexOf(e.data("kind")) >= 0) {
          e.hide();
        } else {
          e.show();
        }
        e.toggleClass(
          "cross-system",
          (selectedSystem === "" || showCrossSystemConnections)
            && isCrossSystem(e, selectedSystem)
        );
      });
    });
    // Hidden selections must clear their details and emphasis; retaining a
    // panel for an element the user can no longer see is misleading. A
    // revision-excluded fact stays mounted (to keep union geometry stable) but
    // is opacity-hidden, so it counts as hidden here too.
    if (selectedElement && (!selectedElement.visible()
        || selectedElement.hasClass("revision-hidden"))) {
      closeAll();
    } else {
      applyEmphasis();
    }
    if (shouldRelayout) runLayout(animate !== false);
  }

  // --- Search ---
  // Debouncing avoids relabeling every canvas element for intermediate keystrokes
  // while preserving immediate-feeling search on ordinary graphs.
  var searchEl = document.getElementById("search");
  var searchTimeout;
  searchEl.addEventListener("input", function () {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(doSearch, 150);
  });

  function doSearch() {
    applyEmphasis();
  }

  // --- Helpers ---
  var detailEl = document.getElementById("detail");
  var detailContent = document.getElementById("detail-content");
  var detailResizeEl = document.getElementById("detail-resize");
  var selectedElement = null;
  var DETAIL_MIN_WIDTH = 240;

  function locationLabel(loc) {
    var r = loc.range;
    return loc.path + ":" + (r.start.line + 1) + ":" + (r.start.character + 1);
  }

  function escHtml(s) {
    // Detail markup is assembled for compactness, so every graph-derived value
    // crosses this boundary before it can become part of that markup.
    var d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
  }

  function badgeHtml(label, colors) {
    return '<span class="kind-badge" style="background:' + colors.bg + ';border:1px solid ' + colors.border + ';color:' + colors.border + '">' + escHtml(label) + '</span>';
  }

  function collectLocations(evidence) {
    var locs = [];
    evidence.forEach(function (ev) {
      if (ev.locations) {
        ev.locations.forEach(function (loc) { locs.push(loc); });
      }
    });
    return locs;
  }

  function sqlForeignKeyRecords(evidence) {
    return evidence.map(function (record) {
      var extensions = record.extensions || {};
      var sqlExtension = extensions["minotaur-sql"] || {};
      var pairs = Array.isArray(sqlExtension.foreign_key_columns)
        ? sqlExtension.foreign_key_columns : [];
      return { locations: record.locations || [], pairs: pairs };
    });
  }

  function sqlForeignKeyDetails(evidence) {
    var records = sqlForeignKeyRecords(evidence);
    var html = '<div class="sql-fk-records">';
    records.forEach(function (record, index) {
      html += '<div class="field sql-fk-record"><div class="field-label">Evidence record '
        + (index + 1) + '</div>';
      if (record.pairs.length > 0) {
        html += '<div class="field-label">Column mappings</div><ul class="sql-fk-pairs">';
        record.pairs.forEach(function (pair) {
          html += '<li><span class="sql-fk-local">' + escHtml(pair.local)
            + '</span> → <span class="sql-fk-referenced">' + escHtml(pair.referenced)
            + '</span></li>';
        });
        html += '</ul>';
      }
      html += '<div class="field-label">Locations</div>';
      if (record.locations.length === 0) {
        html += '<div class="field-value">No source location available</div>';
      } else {
        html += '<ul class="sql-fk-locations">';
        record.locations.forEach(function (location) {
          html += '<li>' + escHtml(locationLabel(location)) + '</li>';
        });
        html += '</ul>';
      }
      html += '</div>';
    });
    return html + '</div>';
  }

  function physicalLocationKey(loc) {
    var r = loc.range;
    return loc.path + "|" + r.start.line + ":" + r.start.character + "-" + r.end.line + ":" + r.end.character;
  }

  function rawCallSites(d, side) {
    if (comparisonMode) {
      return comparisonExcerpts(side).call_sites[d.id] || [];
    }
    return d.call_sites || [];
  }

  function callSitesForEdge(d, side) {
    // Evidence records may independently support the same physical call. The
    // selector represents a location a reader can inspect, not each producer's
    // record, while provenance remains visible as the reason it is supported.
    var sites = new Map();
    rawCallSites(d, side).forEach(function (association) {
      var key = physicalLocationKey(association.location);
      var site = sites.get(key);
      if (!site) {
        site = { location: association.location, provenance: [], caller_start: association.caller_start };
        sites.set(key, site);
      }
      if (site.provenance.indexOf(association.provenance) === -1) {
        site.provenance.push(association.provenance);
      }
      if (site.caller_start === undefined && association.caller_start !== undefined) {
        site.caller_start = association.caller_start;
      }
    });
    return Array.from(sites.values());
  }

  function excerptLines(path, start, end, paths) {
    var excerpt = (paths || excerptPaths)[path];
    if (!excerpt || excerpt.status !== "available") return null;
    var rows = [];
    var cursor = start;
    // The extractor merges stored spans to avoid duplicate source bytes. Build
    // display rows from that sparse representation instead of assuming the
    // requested window is continuous; explicit gap rows prevent false context.
    excerpt.spans.forEach(function (span) {
      var spanStart = span.start;
      var spanEnd = span.start + span.lines.length;
      var from = Math.max(start, spanStart);
      var to = Math.min(end, spanEnd);
      if (from >= to) return;
      if (from > cursor) rows.push({ omitted: true, start: cursor, end: from });
      for (var line = from; line < to; line += 1) {
        rows.push({ line: line, text: span.lines[line - spanStart] });
      }
      cursor = to;
    });
    if (cursor < end) rows.push({ omitted: true, start: cursor, end: end });
    return rows;
  }

  function renderCallSite(site, mode, paths, originNote) {
    var location = site.location;
    var range = location.range;
    var excerpt = (paths || excerptPaths)[location.path];
    var html = '<div class="field"><div class="field-label">Location</div><div class="field-value">' + escHtml(locationLabel(location)) + '</div></div>';
    html += '<div class="field"><div class="field-label">Supporting provenance</div><div class="field-value">' + escHtml(site.provenance.join(", ")) + '</div></div>';
    html += '<div class="excerpt-origin">' + escHtml(
      originNote || "Derived from the source root at visualization time; it may not match the graph analysis snapshot."
    ) + '</div>';
    if (!excerpt || excerpt.status !== "available") {
      html += '<div class="excerpt-unavailable">Source context unavailable: ' + escHtml(excerpt ? excerpt.reason : "no excerpt was embedded") + '</div>';
      return html;
    }
    // The default is bounded in both directions. Prefix mode is only offered
    // when extraction established a real enclosing caller boundary.
    var start = mode === "caller" ? site.caller_start : Math.max(0, range.start.line - 50);
    var end = mode === "caller" ? range.end.line + 1 : range.end.line + 51;
    var rows = excerptLines(location.path, start, end, paths);
    html += '<div class="code-excerpt" aria-label="Source excerpt">';
    rows.forEach(function (row) {
      if (row.omitted) {
        html += '<div class="code-gap">… lines ' + (row.start + 1) + '–' + row.end + ' omitted …</div>';
      } else {
        var highlighted = row.line >= range.start.line && row.line <= range.end.line;
        html += '<div class="code-line' + (highlighted ? ' call-site-highlight' : '') + '"><span class="code-number">' + (row.line + 1) + '</span><span class="code-text">' + escHtml(row.text) + '</span></div>';
      }
    });
    html += '</div>';
    return html;
  }

  function clearDetail() {
    selectedElement = null;
    detailContent.innerHTML = '<div class="empty-state"><h3>Details</h3><p>Select a node or edge to inspect it.</p></div>';
  }

  function setDetailWidth(width) {
    var maximum = Math.floor(window.innerWidth / 2);
    var clamped = Math.max(DETAIL_MIN_WIDTH, Math.min(maximum, width));
    detailEl.style.width = clamped + "px";
    // Flexbox has changed the canvas element's screen position and width, but
    // Cytoscape caches those dimensions for pointer-coordinate translation.
    // Refresh synchronously so the next click is hit-tested against the canvas
    // the user can see; doing it here covers both drag and keyboard resizing.
    cy.resize();
    detailResizeEl.setAttribute("aria-valuemax", maximum);
    detailResizeEl.setAttribute("aria-valuenow", clamped);
  }

  // Pointer capture keeps a drag alive across the full-height handle even when
  // the pointer crosses into the canvas; keyboard support gives the same panel
  // width control to non-pointer users.
  detailResizeEl.addEventListener("pointerdown", function (evt) {
    evt.preventDefault();
    var startX = evt.clientX;
    var startWidth = detailEl.getBoundingClientRect().width;
    detailResizeEl.classList.add("dragging");
    detailResizeEl.setPointerCapture(evt.pointerId);

    function move(pointerEvent) {
      setDetailWidth(startWidth + pointerEvent.clientX - startX);
    }

    function end(pointerEvent) {
      detailResizeEl.classList.remove("dragging");
      detailResizeEl.releasePointerCapture(pointerEvent.pointerId);
      detailResizeEl.removeEventListener("pointermove", move);
      detailResizeEl.removeEventListener("pointerup", end);
      detailResizeEl.removeEventListener("pointercancel", end);
    }

    detailResizeEl.addEventListener("pointermove", move);
    detailResizeEl.addEventListener("pointerup", end);
    detailResizeEl.addEventListener("pointercancel", end);
  });

  detailResizeEl.addEventListener("keydown", function (evt) {
    var current = detailEl.getBoundingClientRect().width;
    if (evt.key === "ArrowLeft") {
      evt.preventDefault();
      setDetailWidth(current - 20);
    } else if (evt.key === "ArrowRight") {
      evt.preventDefault();
      setDetailWidth(current + 20);
    } else if (evt.key === "Home") {
      evt.preventDefault();
      setDetailWidth(DETAIL_MIN_WIDTH);
    } else if (evt.key === "End") {
      evt.preventDefault();
      setDetailWidth(window.innerWidth / 2);
    }
  });

  setDetailWidth(detailEl.getBoundingClientRect().width);

  function closeAll() {
    clearDetail();
    applyEmphasis();
  }

  clearDetail();

  // Initial comparison presentation is data-driven: emphasis is derived from
  // stored statuses, the no-change state from the complete stored boolean, and
  // the summary from the stored system change categories.
  computeChangeEmphasis();
  initComparisonControls();
  renderComparisonSummary();
  applyFilters(false);

  // --- Node click: update details panel ---
  // Details are persistent rather than a popover so evidence has no competing
  // overlay dimensions and remains visible while the graph is explored.
  cy.on("tap", "node", function (evt) {
    selectedElement = evt.target;
    showNodeDetail(evt.target);
    applyEmphasis();
  });

  // --- Edge click: update details panel ---
  cy.on("tap", "edge", function (evt) {
    selectedElement = evt.target;
    showEdgeDetail(evt.target);
    applyEmphasis();
  });

  function showNodeDetail(n) {
    if (comparisonMode) {
      showComparisonNodeDetail(n);
      return;
    }
    var d = n.data();
    var raw = byId.get(d.id);
    var c = CLASS_COLORS[d.node_class] || { bg: "#ddd", border: "#999" };

    var html = badgeHtml(d.node_class, c);
    html += '<h3>' + escHtml(d.label) + '</h3>';

    if (d.symbol_kind) {
      html += '<div class="field"><div class="field-label">Symbol Kind</div>';
      html += '<div class="field-value">' + escHtml(d.symbol_kind) + '</div></div>';
    }
    if (d.path) {
      html += '<div class="field"><div class="field-label">Path</div>';
      html += '<div class="field-value">' + escHtml(d.path) + '</div></div>';
    }
    if (raw && raw.location) {
      html += '<div class="field"><div class="field-label">Location</div>';
      html += '<div class="field-value">' + escHtml(locationLabel(raw.location)) + '</div></div>';
    }
    if (d.reference_text) {
      html += '<div class="field"><div class="field-label">Reference</div>';
      html += '<div class="field-value">' + escHtml(d.reference_text) + '</div></div>';
    }

    // List only visible relationships: the panel describes the filtered graph,
    // not hidden context a user cannot currently select.
    var connected = n.connectedEdges().filter(function (e) { return e.visible(); });
    if (connected.length > 0) {
      html += '<div class="edges-list"><div class="field-label">Connections (' + connected.length + ')</div>';
      connected.forEach(function (e) {
        var ed = e.data();
        var other = e.source().id() === n.id() ? e.target() : e.source();
        var dir = e.source().id() === n.id() ? "→" : "←";
        // Separating relation and target into grid rows lets long qualified
        // targets wrap independently instead of forcing a very wide panel.
        html += '<div class="edge-item"><span class="edge-type">' + escHtml(ed.kind) + ' ' + dir + '</span>';
        html += '<span class="edge-target">' + escHtml(other.data("label")) + '</span></div>';
      });
      html += '</div>';
    }

    detailContent.innerHTML = html;
  }

  function showEdgeDetail(e) {
    if (comparisonMode) {
      showComparisonEdgeDetail(e);
      return;
    }
    var d = e.data();
    var c = EDGE_KIND_COLORS[d.kind] || { bg: "#ddd", border: "#999" };
    var srcLabel = e.source().data("label");
    var tgtLabel = e.target().data("label");
    var locs = collectLocations(d.evidence);
    var sites = d.kind === "calls" ? callSitesForEdge(d, null) : [];

    var html = badgeHtml(d.kind, c);
    html += '<h3>' + escHtml(srcLabel) + ' → ' + escHtml(tgtLabel) + '</h3>';

    html += '<div class="field"><div class="field-label">Provenance</div>';
    html += '<div class="field-value">' + escHtml(d.provenance) + '</div></div>';

    if (d.kind === "sql:foreign-key-to") {
      html += sqlForeignKeyDetails(d.evidence);
    } else if (d.kind === "calls" && sites.length > 0) {
      html += '<div class="field"><div class="field-label">Call sites (' + sites.length + ')</div>';
      html += '<select id="call-site-select" aria-label="Call sites">';
      sites.forEach(function (site, i) {
        html += '<option value="' + i + '">' + (i + 1) + '. ' + escHtml(locationLabel(site.location)) + '</option>';
      });
      html += '</select></div>';
      html += '<div class="field"><div class="field-label">Context mode</div><select id="context-mode" aria-label="Context mode"></select></div>';
      html += '<div id="call-site-detail"></div>';
    } else if (locs.length === 0) {
      html += '<div class="field"><div class="field-label">Location</div>';
      html += '<div class="field-value">No source location available</div></div>';
    } else if (locs.length === 1) {
      html += '<div class="field"><div class="field-label">Location</div>';
      html += '<div class="field-value">' + escHtml(locationLabel(locs[0])) + '</div></div>';
    } else {
      html += '<div class="field"><div class="field-label">Call Sites (' + locs.length + ')</div>';
      html += '<div class="site-tabs" id="site-tabs">';
      locs.forEach(function (_, i) {
        html += '<button class="site-tab' + (i === 0 ? ' active' : '') + '" data-idx="' + i + '">' + (i + 1) + '</button>';
      });
      html += '</div>';
      html += '<div class="field-label">Location</div>';
      html += '<div class="field-value" id="site-location">' + escHtml(locationLabel(locs[0])) + '</div>';
      html += '</div>';
    }

    detailContent.innerHTML = html;

    if (sites.length > 0) {
      var siteSelect = document.getElementById("call-site-select");
      var modeSelect = document.getElementById("context-mode");
      var siteDetail = document.getElementById("call-site-detail");
      function updateSite() {
        var site = sites[Number(siteSelect.value)];
        modeSelect.innerHTML = '<option value="window">Call-site window</option>';
        if (site.caller_start !== undefined) {
          modeSelect.innerHTML += '<option value="caller">Caller start → call</option>';
        }
        siteDetail.innerHTML = renderCallSite(site, modeSelect.value);
        // Wait until the new code rows have layout before scrolling; otherwise
        // a newly selected site can remain off-screen in a long excerpt.
        window.requestAnimationFrame(function () {
          var highlighted = siteDetail.querySelector(".call-site-highlight");
          if (highlighted) highlighted.scrollIntoView({ block: "center" });
        });
      }
      siteSelect.addEventListener("change", updateSite);
      modeSelect.addEventListener("change", function () {
        siteDetail.innerHTML = renderCallSite(sites[Number(siteSelect.value)], modeSelect.value);
        window.requestAnimationFrame(function () {
          var highlighted = siteDetail.querySelector(".call-site-highlight");
          if (highlighted) highlighted.scrollIntoView({ block: "center" });
        });
      });
      updateSite();
    } else if (locs.length > 1) {
      var tabContainer = document.getElementById("site-tabs");
      tabContainer.addEventListener("click", function (evt) {
        var tab = evt.target.closest(".site-tab");
        if (!tab) return;
        var idx = Number(tab.dataset.idx);
        tabContainer.querySelectorAll(".site-tab").forEach(function (t) { t.classList.remove("active"); });
        tab.classList.add("active");
        document.getElementById("site-location").textContent = locationLabel(locs[idx]);
      });
    }
  }

  // --- Comparison details: stored status, sides, and one source excerpt ---
  // Every value below is read from the immutable comparison payload. The panel
  // never decides whether something changed; it only labels what it was told.
  function comparisonStatusField(text) {
    return '<div class="field comparison-status"><div class="field-label">Status</div><div class="field-value">' + escHtml(text) + '</div></div>';
  }

  function reasonSuffix(reasons) {
    if (!reasons || !reasons.length) return "";
    return " · " + reasons.map(titleCase).join(", ");
  }

  function structuralSideFields(record) {
    var html = "";
    ["before", "after"].forEach(function (side) {
      html += '<div class="field"><div class="field-label">' + sideLabel(side) + '</div>';
      if (!sidePresent(record, side)) {
        html += '<div class="field-value structural-absent">Not present</div>';
      } else {
        var value = sidePayload(record, side) || {};
        var parts = [];
        if (value.label) parts.push(value.label);
        if (value.location && value.location.range) {
          parts.push(value.location.path + ":" + (value.location.range.start.line + 1));
        } else if (value.path) {
          parts.push(value.path);
        }
        html += '<div class="field-value">' + escHtml(parts.join(" · ") || "present") + '</div>';
      }
      html += '</div>';
    });
    return html;
  }

  function showComparisonNodeDetail(n) {
    var record = n.data("comparison_record") || {};
    var d = n.data();
    var c = CLASS_COLORS[d.node_class] || { bg: "#ddd", border: "#999" };
    var status = titleCase(record.status || "unchanged");
    if ((record.status || "unchanged") === "unchanged" && changedNodeIds.has(n.id())) {
      // Emphasis because of a changed connection is stated explicitly so the
      // node is never relabeled as changed by the visual treatment alone.
      status += " · Connected to a changed relationship";
    }
    var html = badgeHtml(d.node_class, c);
    html += '<h3>' + escHtml(d.label) + '</h3>';
    html += comparisonStatusField(status);
    if (record.reasons && record.reasons.length) {
      html += '<div class="field"><div class="field-label">Reasons</div><div class="field-value">' + escHtml(record.reasons.map(titleCase).join(", ")) + '</div></div>';
    }
    html += '<div class="field"><div class="field-label">Membership</div><div class="field-value">'
      + "Before: " + escHtml(membershipLabel(record, "before"))
      + " · After: " + escHtml(membershipLabel(record, "after")) + '</div></div>';
    html += structuralSideFields(record);
    detailContent.innerHTML = html;
  }

  function showComparisonEdgeDetail(e) {
    var d = e.data();
    var record = d.comparison_record || {};
    var call = callChangeFor(record.id);
    var c = EDGE_KIND_COLORS[d.kind] || { bg: "#ddd", border: "#999" };
    var html = badgeHtml(d.kind, c);
    html += '<h3>' + escHtml(e.source().data("label")) + ' → ' + escHtml(e.target().data("label")) + '</h3>';
    html += comparisonStatusField(titleCase(record.status || "unchanged") + reasonSuffix(record.reasons));
    if (call) {
      var callText = call.status === "unavailable"
        ? "Call-expression comparison unavailable"
        : titleCase(call.status) + reasonSuffix(call.reasons);
      html += '<div class="field"><div class="field-label">Call expression</div><div class="field-value">' + escHtml(callText) + '</div></div>';
    }
    html += structuralSideFields(record);
    if (d.kind === "calls") {
      html += '<div class="field"><div class="field-label">Source revision</div><select id="source-revision" aria-label="Source revision"></select></div>';
      // Only one side is rendered at a time, so the panel never holds two
      // competing excerpt regions.
      html += '<div id="comparison-side-detail"></div>';
      detailContent.innerHTML = html;
      setupComparisonSourceRegion(e, record);
      return;
    }
    var locs = collectLocations(d.evidence);
    if (locs.length === 0) {
      html += '<div class="field"><div class="field-label">Location</div><div class="field-value">No source location available</div></div>';
    } else {
      html += '<div class="field"><div class="field-label">Location</div><div class="field-value">' + escHtml(locs.map(locationLabel).join(", ")) + '</div></div>';
    }
    detailContent.innerHTML = html;
  }

  function refocus(elementId) {
    var element = document.getElementById(elementId);
    if (element) element.focus();
  }

  function scrollHighlight() {
    window.requestAnimationFrame(function () {
      var detail = document.getElementById("call-site-detail");
      if (!detail) return;
      var highlighted = detail.querySelector(".call-site-highlight");
      // Scroll only the code region, and never move focus away from a control.
      if (highlighted) highlighted.scrollIntoView({ block: "nearest" });
    });
  }

  function sideExcerptUnavailableReason(side) {
    var paths = excerptPathsForSide(side);
    var keys = Object.keys(paths);
    if (!keys.length) return null;
    for (var index = 0; index < keys.length; index += 1) {
      if (paths[keys[index]].status === "available") return null;
    }
    return paths[keys[0]].reason || "captured source bytes are unavailable";
  }

  function setupComparisonSourceRegion(edge, record) {
    var sourceSelect = document.getElementById("source-revision");
    var state = {
      side: record.default_side === "before" ? "before" : "after",
      perSide: {
        before: { site: 0, mode: "window" },
        after: { site: 0, mode: "window" },
      },
    };
    ["before", "after"].forEach(function (side) {
      var option = document.createElement("option");
      option.value = side;
      option.textContent = sideLabel(side) + (sidePresent(record, side) ? "" : " (not present)");
      sourceSelect.appendChild(option);
    });
    sourceSelect.value = state.side;
    renderComparisonSide(edge, record, state);
    sourceSelect.addEventListener("change", function () {
      // Details-only switch: the source revision never changes graph view,
      // layout, zoom, pan, selection, or emphasis.
      state.side = sourceSelect.value;
      renderComparisonSide(edge, record, state);
    });
  }

  function renderComparisonSide(edge, record, state) {
    var container = document.getElementById("comparison-side-detail");
    if (!container) return;
    var side = state.side;
    var sideState = state.perSide[side];
    if (!sidePresent(record, side)) {
      container.innerHTML = '<div class="field"><div class="field-label">' + sideLabel(side) + '</div><div class="field-value structural-absent">Not present</div></div>';
      return;
    }
    var sites = callSitesForEdge(edge.data(), side);
    if (!sites.length) {
      var unavailableReason = sideExcerptUnavailableReason(side);
      container.innerHTML = unavailableReason
        ? '<div class="excerpt-unavailable">Source unavailable: ' + escHtml(unavailableReason) + '</div>'
        : '<div class="field"><div class="field-label">' + sideLabel(side) + '</div><div class="field-value">No call site recorded</div></div>';
      return;
    }
    if (sideState.site >= sites.length) sideState.site = 0;
    var site = sites[sideState.site];
    var modes = site.caller_start === undefined ? ["window"] : ["window", "caller"];
    if (modes.indexOf(sideState.mode) < 0) sideState.mode = "window";
    var html = '<div class="field"><div class="field-label">Call sites (' + sites.length + ')</div><select id="call-site-select" aria-label="Call sites">';
    sites.forEach(function (item, index) {
      html += '<option value="' + index + '"' + (index === sideState.site ? " selected" : "") + '>' + (index + 1) + '. ' + escHtml(locationLabel(item.location)) + '</option>';
    });
    html += '</select></div>';
    html += '<div class="field"><div class="field-label">Context mode</div><select id="context-mode" aria-label="Context mode">';
    modes.forEach(function (mode) {
      html += '<option value="' + mode + '"' + (mode === sideState.mode ? " selected" : "") + '>' + (mode === "caller" ? "Caller start → call" : "Call-site window") + '</option>';
    });
    html += '</select></div>';
    html += '<div id="call-site-detail"></div>';
    container.innerHTML = html;
    document.getElementById("call-site-detail").innerHTML = renderCallSite(
      site, sideState.mode, excerptPathsForSide(side), comparisonOrigin(side)
    );
    var siteSelect = document.getElementById("call-site-select");
    siteSelect.addEventListener("change", function () {
      sideState.site = Number(siteSelect.value);
      renderComparisonSide(edge, record, state);
      refocus("call-site-select");
      scrollHighlight();
    });
    var modeSelect = document.getElementById("context-mode");
    modeSelect.addEventListener("change", function () {
      sideState.mode = modeSelect.value;
      renderComparisonSide(edge, record, state);
      refocus("context-mode");
      scrollHighlight();
    });
    scrollHighlight();
  }

  // --- Comparison summary and no-change presentation ---
  function unavailableExpressionLimitation() {
    return comparisonLimitations.some(function (item) {
      return item && item.code === "call-expression-unavailable";
    });
  }

  function changeSubject(change) {
    var key = Array.isArray(change.key) ? change.key.join(" ") : String(change.key || "");
    var beforeValue = change.old || {};
    var afterValue = change.new || {};
    var beforeLabel = beforeValue.system === undefined || beforeValue.system === null
      ? null : String(beforeValue.system);
    var afterLabel = afterValue.system === undefined || afterValue.system === null
      ? null : String(afterValue.system);
    if (beforeLabel !== null || afterLabel !== null) {
      return key + " — " + (beforeLabel === null ? "unassigned" : beforeLabel)
        + " -> " + (afterLabel === null ? "unassigned" : afterLabel);
    }
    return key;
  }

  function comparisonSummaryLines() {
    var lines = [];
    (comparisonPayload.added_systems || []).forEach(function (name) {
      lines.push("system added: " + name);
    });
    (comparisonPayload.removed_systems || []).forEach(function (name) {
      lines.push("system removed: " + name);
    });
    [
      ["membership_changes", "membership"],
      ["surface_changes", "surface"],
      ["consumer_changes", "consumer"],
      ["dependency_changes", "dependency"],
      ["boundary_changes", "boundary"],
    ].forEach(function (pair) {
      (comparisonPayload[pair[0]] || []).forEach(function (change) {
        lines.push(pair[1] + " " + titleCase(change.kind) + ": " + changeSubject(change));
      });
    });
    (comparisonPayload.nodes || []).forEach(function (node) {
      if (recordChanged(node)) lines.push("node " + titleCase(node.status) + ": " + node.id);
    });
    (comparisonPayload.relationships || []).forEach(function (relationship) {
      if (recordChanged(relationship)) {
        lines.push("relationship " + titleCase(relationship.status) + ": " + relationship.id);
      }
    });
    comparisonCallChanges().forEach(function (change) {
      if (recordChanged(change)) {
        lines.push("call " + titleCase(change.status) + ": " + (change.id || change.relationship_id));
      }
    });
    return lines;
  }

  // The captured revision identities are part of the report itself: a saved
  // comparison keeps showing the names and resolved commit IDs it was
  // generated from even after a branch moves. Ordinary graph views have no
  // comparison payload, so this element stays hidden there.
  function renderComparisonRevisions() {
    var container = document.getElementById("comparison-revisions");
    if (!container) return;
    var revisions = comparisonPayload.revisions || {};
    var entries = [];
    if (revisions.old) entries.push("Before: " + String(revisions.old));
    if (revisions.new) entries.push("After: " + String(revisions.new));
    container.innerHTML = entries.map(function (entry) {
      return '<span class="comparison-revision">' + escHtml(entry) + "</span>";
    }).join("");
    container.hidden = entries.length === 0;
  }

  function renderComparisonSummary() {
    if (!comparisonMode || !comparisonPayload) return;
    document.getElementById("comparison-header").hidden = false;
    document.getElementById("comparison-legend").hidden = false;
    renderComparisonRevisions();
    var lines = comparisonSummaryLines();
    document.getElementById("comparison-summary-body").innerHTML = lines.length
      ? "<ul>" + lines.map(function (line) {
        return "<li>" + escHtml(line) + "</li>";
      }).join("") + "</ul>"
      : "<p>No stored structural change categories.</p>";
    if (comparisonLimitations.length) {
      var noticesEl = document.getElementById("comparison-notices");
      noticesEl.hidden = false;
      noticesEl.innerHTML = comparisonLimitations.map(function (item) {
        return '<div class="comparison-notice">' + escHtml(
          "limitation " + item.side + ": " + item.code + ": " + item.message
        ) + "</div>";
      }).join("");
    }
    if (noChangeState) {
      var noChangeEl = document.getElementById("comparison-no-change");
      noChangeEl.hidden = false;
      noChangeEl.textContent = "No structural changes found"
        + (unavailableExpressionLimitation()
          ? " · Call-expression comparison unavailable" : "");
    }
  }

  function initComparisonControls() {
    if (!comparisonMode) return;
    document.getElementById("emphasis-control").hidden = false;
    if (noChangeState) {
      emphasisEl.checked = false;
      emphasisEl.disabled = true;
    } else {
      emphasisEl.checked = true;
      emphasisEl.disabled = false;
    }
    emphasisEl.addEventListener("change", applyEmphasis);
  }

  // --- Canvas click: close everything ---
  // Canvas dismissal restores a neutral state without hiding the details panel,
  // making its empty state a stable orientation point on the left of the view.
  cy.on("tap", function (evt) {
    if (evt.target === cy) {
      closeAll();
    }
  });

  // --- Edge hover tooltip ---
  var tooltipEl = document.getElementById("tooltip");

  cy.on("mouseover", "edge", function (evt) {
    var e = evt.target;
    var d = e.data();
    var text = d.kind;
    var locs = collectLocations(d.evidence);
    if (locs.length > 0) {
      locs.forEach(function (loc) { text += "\n" + locationLabel(loc); });
    }
    tooltipEl.textContent = text;
    tooltipEl.style.display = "block";
  });

  cy.on("mouseout", "edge", function () {
    tooltipEl.style.display = "none";
  });

  cy.on("mousemove", function (evt) {
    if (tooltipEl.style.display === "block") {
      tooltipEl.style.left = (evt.originalEvent.offsetX + 14) + "px";
      tooltipEl.style.top = (evt.originalEvent.offsetY + 14) + "px";
    }
  });

  // --- Layout direction ---
  var DIR_CYCLE = ["TB", "LR", "BT", "RL"];
  var DIR_ARROWS = { "TB": "⬇", "LR": "➡", "BT": "⬆", "RL": "⬅" };
  var dirIdx = 0;
  var dirBtn = document.getElementById("btn-direction");
  dirBtn.textContent = DIR_ARROWS[layoutDir];
  dirBtn.addEventListener("click", function () {
    dirIdx = (dirIdx + 1) % DIR_CYCLE.length;
    layoutDir = DIR_CYCLE[dirIdx];
    dirBtn.textContent = DIR_ARROWS[layoutDir];
    runLayout();
  });

  document.getElementById("btn-fit").addEventListener("click", function () {
    fitVisible();
  });

  var zoomSlider = document.getElementById("zoom-speed");
  zoomSlider.addEventListener("input", function () {
    var renderer = cy._private.renderer;
    if (renderer) renderer.wheelSensitivity = Number(zoomSlider.value) / 5;
  });

  function stopLayout() {
    if (activeLayout) activeLayout.stop();
    cy.elements().stop(true, false);
    cy.stop(true, false);
  }

  function fitVisible() {
    stopLayout();
    var visible = cy.elements(":visible");
    if (visible.nodes().length) cy.fit(visible, 30);
  }

  function runLayout(animate) {
    stopLayout();
    if (comparisonMode) {
      runComparisonLayout(animate);
      return;
    }
    var visible = cy.elements(":visible");
    if (!visible.nodes().length) return;
    var selectedSystem = systemFilterEl.value;
    if (selectedSystem !== "") {
      runFocusedLayout(
        visible, selectedSystem, crossSystemConnectionsEl.checked, animate
      );
      return;
    }
    // Passing the collection keeps hidden nodes and edges out of Dagre's ranks
    // while preserving their identities and evidence for later restoration.
    activeLayout = visible.layout({
      name: "dagre", rankDir: layoutDir, nodeSep: 40, rankSep: 60, edgeSep: 15,
      padding: 30, animate: animate !== false, animationDuration: 300
    });
    activeLayout.run();
  }

  function checkedComparisonNodes() {
    var hiddenKinds = [];
    kindsEl.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      if (!cb.checked) hiddenKinds.push(cb.dataset.kind);
    });
    return cy.nodes().not(".system-container").filter(function (node) {
      return hiddenKinds.indexOf(node.data("node_class")) < 0
        && recordPresence(node.data("comparison_record"), "combined");
    });
  }

  function checkedComparisonEdges() {
    var hiddenKinds = [];
    edgesEl.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      if (!cb.checked) hiddenKinds.push(cb.dataset.edgekind);
    });
    return cy.edges().filter(function (edge) {
      return hiddenKinds.indexOf(edge.data("kind")) < 0
        && edgePresentInView(edge, "combined");
    });
  }

  function comparisonLayoutEligible(node, selectedSystem, showCross) {
    if (selectedSystem === "") return true;
    if (nodeBelongsTo(node, selectedSystem, "before")
        || nodeBelongsTo(node, selectedSystem, "after")) return true;
    if (!showCross) return false;
    return checkedComparisonEdges().some(function (edge) {
      return (edge.source().id() === node.id() || edge.target().id() === node.id())
        && edgeTouchesSystemInView(edge, selectedSystem, "combined");
    });
  }

  function comparisonLayoutEdgeEligible(edge, selectedSystem, showCross, nodes) {
    if (!nodes.contains(edge.source()) || !nodes.contains(edge.target())) return false;
    if (selectedSystem === "") return true;
    if (showCross) return edgeTouchesSystemInView(edge, selectedSystem, "combined");
    return edgeHasInternalInView(edge, selectedSystem, "combined");
  }

  function runComparisonLayout(animate) {
    var selectedSystem = systemFilterEl.value;
    var showCrossSystemConnections = selectedSystem !== "" && crossSystemConnectionsEl.checked;
    var nodes = checkedComparisonNodes().filter(function (node) {
      return comparisonLayoutEligible(node, selectedSystem, showCrossSystemConnections);
    });
    var edges = checkedComparisonEdges().filter(function (edge) {
      return comparisonLayoutEdgeEligible(edge, selectedSystem, showCrossSystemConnections, nodes);
    });
    var layoutElements = nodes.union(edges);
    layoutRuns += 1;
    var cacheKey = [layoutDir, selectedSystem, showCrossSystemConnections,
      nodes.map(function (node) { return node.id(); }).sort().join(","),
      edges.map(function (edge) { return edge.id(); }).sort().join(",")].join("|");
    var cached = comparisonLayoutCache.get(cacheKey);
    if (cached) {
      nodes.forEach(function (node) {
        var position = cached.positions[node.id()];
        if (position) node.position(position);
      });
      if (cached.zoom !== undefined) cy.zoom(cached.zoom);
      if (cached.pan) cy.pan(cached.pan);
      if (selectedSystem !== "" && showCrossSystemConnections) createSystemContainers(selectedSystem);
      return;
    }
    if (!nodes.length) return;
    activeLayout = layoutElements.layout({
      name: "dagre", rankDir: layoutDir, nodeSep: 40, rankSep: 60, edgeSep: 15,
      padding: 30, fit: true, animate: animate !== false, animationDuration: 300
    });
    activeLayout.one("layoutstop", function () {
      var positions = {};
      nodes.forEach(function (node) { positions[node.id()] = node.position(); });
      comparisonLayoutCache.set(cacheKey, {
        positions: positions, zoom: cy.zoom(), pan: cy.pan()
      });
      if (selectedSystem !== "" && showCrossSystemConnections) createSystemContainers(selectedSystem);
    });
    activeLayout.run();
  }

  function runFocusedLayout(visible, selectedSystem, showCrossSystemConnections, animate) {
    var selectedNodes = visible.nodes().filter(function (node) {
      return node.data("system") === selectedSystem;
    });
    if (!selectedNodes.length) return;
    var internalEdges = visible.edges().filter(function (edge) {
      return edge.source().data("system") === selectedSystem
        && edge.target().data("system") === selectedSystem;
    });
    var internal = selectedNodes.union(internalEdges);
    activeLayout = internal.layout({
      name: "dagre", rankDir: layoutDir, nodeSep: 40, rankSep: 60, edgeSep: 15,
      padding: 30, fit: false, animate: animate !== false, animationDuration: 300
    });
    activeLayout.one("layoutstop", function () {
      if (systemFilterEl.value !== selectedSystem
          || crossSystemConnectionsEl.checked !== showCrossSystemConnections) return;
      var boundaryNodes = visible.nodes().difference(selectedNodes);
      var selectedBox = selectedNodes.boundingBox({ includeLabels: true });
      var centerX = (selectedBox.x1 + selectedBox.x2) / 2;
      var centerY = (selectedBox.y1 + selectedBox.y2) / 2;
      if (!showCrossSystemConnections) {
        cy.fit(selectedNodes, 30);
        var selectedZoom = cy.zoom();
        cy.pan({
          x: cy.width() / 2 - centerX * selectedZoom,
          y: cy.height() / 2 - centerY * selectedZoom
        });
        return;
      }
      var boundaryGroups = new Map();
      boundaryNodes.forEach(function (node) {
        var name = node.data("system") || "External / Unassigned";
        if (!boundaryGroups.has(name)) boundaryGroups.set(name, []);
        boundaryGroups.get(name).push(node);
      });
      var groupNames = Array.from(boundaryGroups.keys()).sort();
      var largestGroupSide = Math.max(1, ...groupNames.map(function (name) {
        return Math.ceil(Math.sqrt(boundaryGroups.get(name).length));
      }));
      var horizontal = layoutDir === "LR" || layoutDir === "RL";
      var radius = (horizontal ? selectedBox.w : selectedBox.h) / 2
        + Math.max(240, largestGroupSide * 90 + groupNames.length * 25);
      var startAngle = layoutDir === "LR" ? 0
        : layoutDir === "RL" ? Math.PI
        : layoutDir === "BT" ? Math.PI / 2
        : -Math.PI / 2;
      groupNames.forEach(function (name, groupIndex) {
        var nodes = boundaryGroups.get(name).sort(function (left, right) {
          return left.id().localeCompare(right.id());
        });
        var columns = Math.ceil(Math.sqrt(nodes.length));
        var rows = Math.ceil(nodes.length / columns);
        var angle = startAngle + (2 * Math.PI * groupIndex / groupNames.length);
        var groupCenterX = centerX + radius * Math.cos(angle);
        var groupCenterY = centerY + radius * Math.sin(angle);
        nodes.forEach(function (node, nodeIndex) {
          var column = nodeIndex % columns;
          var row = Math.floor(nodeIndex / columns);
          node.position({
            x: groupCenterX + (column - (columns - 1) / 2) * 160,
            y: groupCenterY + (row - (rows - 1) / 2) * 90
          });
        });
      });
      createSystemContainers(selectedSystem);
      var focusedVisible = cy.elements(":visible");
      cy.fit(focusedVisible, 30);
      var selectedContainer = cy.getElementById("system-container:" + selectedSystem);
      var selectedContainerBox = selectedContainer.boundingBox({ includeLabels: true });
      centerX = (selectedContainerBox.x1 + selectedContainerBox.x2) / 2;
      centerY = (selectedContainerBox.y1 + selectedContainerBox.y2) / 2;
      var zoom = cy.zoom();
      cy.pan({ x: cy.width() / 2 - centerX * zoom, y: cy.height() / 2 - centerY * zoom });
    });
    activeLayout.run();
  }

  // --- Keyboard shortcuts ---
  document.addEventListener("keydown", function (evt) {
    if (evt.target.tagName === "INPUT") {
      if (evt.key === "Escape") { searchEl.value = ""; searchEl.blur(); doSearch(); }
      return;
    }
    if (evt.key === "Escape") {
      closeAll();
    } else if (evt.key === "f") {
      fitVisible();
    }
  });

  // Kept deliberately small for browser integration tests and local artifact
  // inspection; callers can observe the rendered graph without mutating state.
  window.minotaurVisualizer = {
    cy: cy,
    activeTheme: function () { return { name: activeThemeName, selected: activeTheme.selected }; },
    comparison: comparisonMode,
    revisionView: function () { return revisionView; },
    layoutRuns: function () { return layoutRuns; },
    layoutCacheSize: function () { return comparisonLayoutCache.size; }
  };
}());
