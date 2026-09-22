(function () {
  "use strict";
  var payload = JSON.parse(document.getElementById("minotaur-presentation").textContent);
  var graph = payload.graph || { nodes: [], relationships: [] };
  var comparisonPayload = payload.comparison || null;
  var comparisonMode = Boolean(
    comparisonPayload && Array.isArray(graph.nodes) && graph.nodes.some(function (node) {
      return Object.prototype.hasOwnProperty.call(node, "before")
        || Object.prototype.hasOwnProperty.call(node, "after");
    })
  );
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
    if (Array.isArray(value)) return value[0] || null;
    return value && typeof value === "object" ? value : null;
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

  function sideSystems(record, side) {
    if (!record) return [];
    if (comparisonMode && !sidePresent(record, side)) return [];
    var payloadValue = sidePayload(record, side);
    var candidates = [];
    if (payloadValue) {
      candidates = payloadValue.systems || payloadValue.involved_systems || payloadValue.system || [];
    }
    if (typeof candidates === "string") return [candidates];
    if (Array.isArray(candidates) && candidates.length) {
      return candidates.filter(function (name) { return typeof name === "string"; });
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
    return edgePresentInView(edge, side)
      && nodeBelongsTo(edge.source(), system, side)
      && nodeBelongsTo(edge.target(), system, side);
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

  function edgeTouchesSystemInView(edge, system, view) {
    if (!comparisonMode) {
      return edge.source().data("system") === system || edge.target().data("system") === system;
    }
    if (view === "combined") {
      return nodeBelongsTo(edge.source(), system, "before")
        || nodeBelongsTo(edge.source(), system, "after")
        || nodeBelongsTo(edge.target(), system, "before")
        || nodeBelongsTo(edge.target(), system, "after");
    }
    return nodeBelongsTo(edge.source(), system, view) || nodeBelongsTo(edge.target(), system, view);
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
        var systems = node.data("systems") || [];
        name = systems[0] || name;
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
      groups.get(name).move({ parent: id });
    });
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

  applyFilters(false);

  function applyFilters(animate, options) {
    options = options || {};
    var shouldRelayout = options.layout !== false;
    if (!comparisonMode || shouldRelayout) clearSystemContainers();
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
              && edgeTouchesSystemInView(edge, selectedSystem, currentView)) visibleNodeIds.add(target.id());
          if (targetSelected && eligibleNodes.contains(source)
              && edgeTouchesSystemInView(edge, selectedSystem, currentView)) visibleNodeIds.add(source.id());
        });
      }
    }
    cy.batch(function () {
      cy.nodes().forEach(function (node) {
        if (visibleNodeIds.has(node.id())) { node.show(); } else { node.hide(); }
        node.toggleClass(
          "outside-system",
          showCrossSystemConnections && !nodeBelongsInView(
            node, selectedSystem, comparisonMode ? revisionView : "combined"
          )
        );
      });
      cy.edges().forEach(function (e) {
        var srcHidden = !e.source().visible();
        var tgtHidden = !e.target().visible();
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
    // panel for an element the user can no longer see is misleading.
    if (selectedElement && !selectedElement.visible()) {
      closeAll();
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
    var q = searchEl.value.trim().toLowerCase();
    if (!q) {
      cy.nodes().not(".system-container").removeClass("dimmed highlighted");
      cy.edges().removeClass("dimmed");
      return;
    }
    cy.batch(function () {
      cy.nodes().not(".system-container").forEach(function (n) {
        var label = (n.data("label") || "").toLowerCase();
        var path = (n.data("path") || "").toLowerCase();
        var ref = (n.data("reference_text") || "").toLowerCase();
        if (label.indexOf(q) >= 0 || path.indexOf(q) >= 0 || ref.indexOf(q) >= 0) {
          n.removeClass("dimmed").addClass("highlighted");
        } else {
          n.addClass("dimmed").removeClass("highlighted");
        }
      });
      cy.edges().forEach(function (e) {
        if (e.source().hasClass("dimmed") && e.target().hasClass("dimmed")) {
          e.addClass("dimmed");
        } else {
          e.removeClass("dimmed");
        }
      });
    });
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

  function physicalLocationKey(loc) {
    var r = loc.range;
    return loc.path + "|" + r.start.line + ":" + r.start.character + "-" + r.end.line + ":" + r.end.character;
  }

  function callSitesForEdge(d) {
    // Evidence records may independently support the same physical call. The
    // selector represents a location a reader can inspect, not each producer's
    // record, while provenance remains visible as the reason it is supported.
    var sites = new Map();
    (d.call_sites || []).forEach(function (association) {
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

  function excerptLines(path, start, end) {
    var excerpt = excerptPaths[path];
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

  function renderCallSite(site, mode) {
    var location = site.location;
    var range = location.range;
    var excerpt = excerptPaths[location.path];
    var html = '<div class="field"><div class="field-label">Location</div><div class="field-value">' + escHtml(locationLabel(location)) + '</div></div>';
    html += '<div class="field"><div class="field-label">Supporting provenance</div><div class="field-value">' + escHtml(site.provenance.join(", ")) + '</div></div>';
    html += '<div class="excerpt-origin">Derived from the source root at visualization time; it may not match the graph analysis snapshot.</div>';
    if (!excerpt || excerpt.status !== "available") {
      html += '<div class="excerpt-unavailable">Source context unavailable: ' + escHtml(excerpt ? excerpt.reason : "no excerpt was embedded") + '</div>';
      return html;
    }
    // The default is bounded in both directions. Prefix mode is only offered
    // when extraction established a real enclosing caller boundary.
    var start = mode === "caller" ? site.caller_start : Math.max(0, range.start.line - 50);
    var end = mode === "caller" ? range.end.line + 1 : range.end.line + 51;
    var rows = excerptLines(location.path, start, end);
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

  function clearHighlights() {
    cy.elements().removeClass("highlighted faded");
  }

  function closeAll() {
    clearDetail();
    clearHighlights();
  }

  clearDetail();

  // --- Node click: update details panel ---
  // Details are persistent rather than a popover so evidence has no competing
  // overlay dimensions and remains visible while the graph is explored.
  cy.on("tap", "node", function (evt) {
    var n = evt.target;
    selectedElement = n;
    showNodeDetail(n);

    var neighborhood = n.neighborhood();
    cy.batch(function () {
      cy.elements().addClass("faded").removeClass("highlighted");
      n.addClass("highlighted").removeClass("faded");
      neighborhood.addClass("highlighted").removeClass("faded");
    });
  });

  // --- Edge click: update details panel ---
  cy.on("tap", "edge", function (evt) {
    var e = evt.target;
    selectedElement = e;
    showEdgeDetail(e);

    cy.batch(function () {
      cy.elements().addClass("faded").removeClass("highlighted");
      e.addClass("highlighted").removeClass("faded");
      e.source().addClass("highlighted").removeClass("faded");
      e.target().addClass("highlighted").removeClass("faded");
    });
  });

  function showNodeDetail(n) {
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
    var d = e.data();
    var c = EDGE_KIND_COLORS[d.kind] || { bg: "#ddd", border: "#999" };
    var srcLabel = e.source().data("label");
    var tgtLabel = e.target().data("label");
    var locs = collectLocations(d.evidence);
    var sites = d.kind === "calls" ? callSitesForEdge(d) : [];

    var html = badgeHtml(d.kind, c);
    html += '<h3>' + escHtml(srcLabel) + ' → ' + escHtml(tgtLabel) + '</h3>';

    html += '<div class="field"><div class="field-label">Provenance</div>';
    html += '<div class="field-value">' + escHtml(d.provenance) + '</div></div>';

    if (d.kind === "calls" && sites.length > 0) {
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
