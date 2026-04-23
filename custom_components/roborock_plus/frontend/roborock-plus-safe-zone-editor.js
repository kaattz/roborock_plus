class RoborockPlusSafeZoneEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._vacuumEntities = [];
    this._vacuumEntityId = "";
    this._context = null;
    this._suggestion = null;
    this._draftRect = null;
    this._dragStart = null;
    this._loadedSources = false;
    this._error = "";
    this._params = {
      cabinet_direction: "east",
      safe_distance_front: 2500,
      safe_half_width: 1200,
      close_margin: 300,
    };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loadedSources) {
      this._loadedSources = true;
      this._loadEntitySources();
    }
    this._render();
  }

  async _callService(domain, service, target = {}, serviceData = {}) {
    return this._hass.callWS({
      type: "call_service",
      domain,
      service,
      target,
      service_data: serviceData,
      return_response: true,
    });
  }

  async _loadEntitySources() {
    if (!this._hass) return;
    const sources = await this._hass.callWS({ type: "entity/source" });
    this._vacuumEntities = Object.entries(sources)
      .filter(
        ([entityId, source]) =>
          entityId.startsWith("vacuum.") && source.domain === "roborock_plus"
      )
      .map(([entityId]) => entityId)
      .sort();
    if (!this._vacuumEntityId && this._vacuumEntities.length > 0) {
      this._vacuumEntityId = this._vacuumEntities[0];
      await this._loadContext();
      return;
    }
    this._render();
  }

  _currentImageUrl() {
    if (!this._hass || !this._context) return null;
    if (this._context.image_url) return this._context.image_url;
    if (!this._context.image_entity_id) return null;
    const state = this._hass.states[this._context.image_entity_id];
    return state?.attributes?.entity_picture || null;
  }

  _mapToImage(vacuumPoint) {
    const calibration = this._context?.calibration;
    if (!calibration || calibration.length < 3) return null;
    const [p0, p1, p2] = calibration;
    const mapX =
      p0.map.x +
      ((vacuumPoint.x - p0.vacuum.x) * (p1.map.x - p0.map.x)) /
        (p1.vacuum.x - p0.vacuum.x);
    const mapY =
      p0.map.y +
      ((vacuumPoint.y - p0.vacuum.y) * (p2.map.y - p0.map.y)) /
        (p2.vacuum.y - p0.vacuum.y);
    return { x: mapX, y: mapY };
  }

  _imageToMap(imagePoint) {
    const calibration = this._context?.calibration;
    if (!calibration || calibration.length < 3) return null;
    const [p0, p1, p2] = calibration;
    const vacuumX =
      p0.vacuum.x +
      ((imagePoint.x - p0.map.x) * (p1.vacuum.x - p0.vacuum.x)) /
        (p1.map.x - p0.map.x);
    const vacuumY =
      p0.vacuum.y +
      ((imagePoint.y - p0.map.y) * (p2.vacuum.y - p0.vacuum.y)) /
        (p2.map.y - p0.map.y);
    return { x: vacuumX, y: vacuumY };
  }

  _zoneToDisplayRect(zone) {
    if (!zone) return null;
    const p1 = this._mapToImage({ x: zone.min_x, y: zone.min_y });
    const p2 = this._mapToImage({ x: zone.max_x, y: zone.max_y });
    if (!p1 || !p2) return null;
    const width = Math.abs(p2.x - p1.x);
    const height = Math.abs(p2.y - p1.y);
    return {
      x: Math.min(p1.x, p2.x),
      y: Math.min(p1.y, p2.y),
      width,
      height,
    };
  }

  async _loadContext() {
    if (!this._vacuumEntityId) return;
    try {
      const result = await this._callService(
        "roborock_plus",
        "get_safe_zone_editor_context",
        {
          entity_id: this._vacuumEntityId,
        }
      );
      this._context = result.response;
      this._draftRect = null;
      this._error = this._currentImageUrl()
        ? ""
        : "已加载上下文，但没有找到当前地图图像实体。";
    } catch (err) {
      this._error = err?.message || String(err);
    }
    this._render();
  }

  async _loadSuggestion() {
    if (!this._vacuumEntityId) return;
    const result = await this._callService(
      "roborock_plus",
      "get_safe_zone_suggestion",
      { entity_id: this._vacuumEntityId },
      this._params
    );
    this._suggestion = result.response;
    this._draftRect = this._zoneToDisplayRect(this._suggestion);
    this._render();
  }

  async _saveDraft() {
    if (!this._vacuumEntityId) return;
    const rect = this._draftRect || this._zoneToDisplayRect(this._suggestion);
    if (!rect) return;
    const p1 = this._imageToMap({
      x: rect.x,
      y: rect.y,
    });
    const p2 = this._imageToMap({
      x: rect.x + rect.width,
      y: rect.y + rect.height,
    });
    if (!p1 || !p2) return;
    await this._hass.callService("roborock_plus", "set_safe_zone", {
      entity_id: this._vacuumEntityId,
      min_x: Math.round(Math.min(p1.x, p2.x)),
      max_x: Math.round(Math.max(p1.x, p2.x)),
      min_y: Math.round(Math.min(p1.y, p2.y)),
      max_y: Math.round(Math.max(p1.y, p2.y)),
    });
    await this._loadContext();
  }

  async _clearZone() {
    if (!this._vacuumEntityId) return;
    await this._hass.callService("roborock_plus", "clear_safe_zone", {
      entity_id: this._vacuumEntityId,
    });
    await this._loadContext();
  }

  _pointerToViewBox(event) {
    const svg = this.shadowRoot.getElementById("editor-svg");
    const rect = svg.getBoundingClientRect();
    const width = this._context?.image_meta?.width || rect.width || 1;
    const height = this._context?.image_meta?.height || rect.height || 1;
    return {
      x: ((event.clientX - rect.left) / rect.width) * width,
      y: ((event.clientY - rect.top) / rect.height) * height,
    };
  }

  _onPointerDown(event) {
    this._dragStart = this._pointerToViewBox(event);
    this._draftRect = {
      x: this._dragStart.x,
      y: this._dragStart.y,
      width: 0,
      height: 0,
    };
    this._render();
  }

  _onPointerMove(event) {
    if (!this._dragStart) return;
    const point = this._pointerToViewBox(event);
    this._draftRect = {
      x: Math.min(this._dragStart.x, point.x),
      y: Math.min(this._dragStart.y, point.y),
      width: Math.abs(point.x - this._dragStart.x),
      height: Math.abs(point.y - this._dragStart.y),
    };
    this._render();
  }

  _onPointerUp() {
    this._dragStart = null;
  }

  _render() {
    const imageUrl = this._currentImageUrl();
    const suggestionRect = this._zoneToDisplayRect(this._suggestion);
    const savedRect = this._zoneToDisplayRect(this._context?.safe_zone);
    const currentPos = this._context?.current_position
      ? this._mapToImage(this._context.current_position)
      : null;

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; padding: 16px; }
        .wrap { display: grid; gap: 16px; }
        .controls { display: grid; gap: 12px; max-width: 520px; }
        .row { display: grid; grid-template-columns: 160px 1fr; gap: 8px; align-items: center; }
        .editor { position: relative; display: inline-block; border: 1px solid #444; }
        img { display: block; max-width: 100%; height: auto; }
        svg { position: absolute; inset: 0; width: 100%; height: 100%; cursor: crosshair; }
        button { padding: 8px 12px; }
        .actions { display: flex; gap: 8px; flex-wrap: wrap; }
        .hint { color: #888; font-size: 14px; }
      </style>
      <div class="wrap">
        <div class="controls">
          <div class="row">
            <label>Vacuum entity</label>
            <select id="vacuum-select">
              <option value="">请选择</option>
              ${this._vacuumEntities
                .map(
                  (entityId) =>
                    `<option value="${entityId}" ${entityId === this._vacuumEntityId ? "selected" : ""}>${entityId}</option>`
                )
                .join("")}
            </select>
          </div>
          <div class="row"><label>Direction</label><select id="direction">
            ${["east", "west", "north", "south"]
              .map(
                (value) =>
                  `<option value="${value}" ${value === this._params.cabinet_direction ? "selected" : ""}>${value}</option>`
              )
              .join("")}
          </select></div>
          <div class="row"><label>Safe distance</label><input id="safe-distance" type="number" value="${this._params.safe_distance_front}"></div>
          <div class="row"><label>Half width</label><input id="half-width" type="number" value="${this._params.safe_half_width}"></div>
          <div class="row"><label>Close margin</label><input id="close-margin" type="number" value="${this._params.close_margin}"></div>
          <div class="actions">
            <button id="load-context">加载地图</button>
            <button id="load-suggestion">生成建议框</button>
            <button id="save-zone">保存框选</button>
            <button id="clear-zone">清空安全区</button>
          </div>
          <div class="hint">先加载地图，再生成建议框，最后可手动拖拽框选后保存。</div>
          ${this._error ? `<div class="hint" style="color:#ff6b6b;">${this._error}</div>` : ""}
        </div>
        ${
          imageUrl
            ? `<div class="editor">
                <img id="map-image" src="${imageUrl}">
                <svg id="editor-svg" viewBox="0 0 ${this._context?.image_meta?.width || 1} ${this._context?.image_meta?.height || 1}">
                  ${suggestionRect ? `<rect x="${suggestionRect.x}" y="${suggestionRect.y}" width="${suggestionRect.width}" height="${suggestionRect.height}" fill="rgba(0,128,255,0.15)" stroke="#00aaff" stroke-dasharray="6 4"></rect>` : ""}
                  ${savedRect ? `<rect x="${savedRect.x}" y="${savedRect.y}" width="${savedRect.width}" height="${savedRect.height}" fill="rgba(0,255,0,0.15)" stroke="#00cc66" stroke-width="2"></rect>` : ""}
                  ${this._draftRect ? `<rect x="${this._draftRect.x}" y="${this._draftRect.y}" width="${this._draftRect.width}" height="${this._draftRect.height}" fill="rgba(255,165,0,0.15)" stroke="#ff9900" stroke-width="2"></rect>` : ""}
                  ${currentPos ? `<circle cx="${currentPos.x}" cy="${currentPos.y}" r="8" fill="#ffffff" stroke="#111111" stroke-width="2"></circle>` : ""}
                </svg>
              </div>`
            : `<div class="hint">请选择 vacuum 实体并点击“加载地图”。</div>`
        }
      </div>
    `;

    this.shadowRoot.getElementById("vacuum-select")?.addEventListener("change", (ev) => {
      this._vacuumEntityId = ev.target.value;
      this._context = null;
      this._suggestion = null;
      this._draftRect = null;
    });
    this.shadowRoot.getElementById("direction")?.addEventListener("change", (ev) => {
      this._params.cabinet_direction = ev.target.value;
    });
    this.shadowRoot.getElementById("safe-distance")?.addEventListener("change", (ev) => {
      this._params.safe_distance_front = Number(ev.target.value);
    });
    this.shadowRoot.getElementById("half-width")?.addEventListener("change", (ev) => {
      this._params.safe_half_width = Number(ev.target.value);
    });
    this.shadowRoot.getElementById("close-margin")?.addEventListener("change", (ev) => {
      this._params.close_margin = Number(ev.target.value);
    });
    this.shadowRoot.getElementById("load-context")?.addEventListener("click", () => this._loadContext());
    this.shadowRoot.getElementById("load-suggestion")?.addEventListener("click", () => this._loadSuggestion());
    this.shadowRoot.getElementById("save-zone")?.addEventListener("click", () => this._saveDraft());
    this.shadowRoot.getElementById("clear-zone")?.addEventListener("click", () => this._clearZone());

    const svg = this.shadowRoot.getElementById("editor-svg");
    if (svg) {
      svg.addEventListener("pointerdown", (ev) => this._onPointerDown(ev));
      svg.addEventListener("pointermove", (ev) => this._onPointerMove(ev));
      svg.addEventListener("pointerup", () => this._onPointerUp());
      svg.addEventListener("pointerleave", () => this._onPointerUp());
    }
  }
}

if (!customElements.get("roborock-plus-safe-zone-editor")) {
  customElements.define(
    "roborock-plus-safe-zone-editor",
    RoborockPlusSafeZoneEditor
  );
}
