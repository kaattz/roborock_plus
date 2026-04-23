class RoborockPlusSafeZoneEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._vacuumEntities = [];
    this._imageEntities = [];
    this._vacuumEntityId = "";
    this._imageEntityId = "";
    this._context = null;
    this._draftRect = null;
    this._dragStart = null;
    this._activePointerId = null;
    this._loadedSources = false;
    this._error = "";
    this._notice = "";
    this._imageNaturalSize = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loadedSources) {
      this._loadedSources = true;
      this._loadEntitySources();
    }
    this._render();
  }

  _imageSelectionStorageKey(vacuumEntityId) {
    return `roborock_plus:image_entity:${vacuumEntityId}`;
  }

  _loadPersistedImageEntity(vacuumEntityId) {
    if (!vacuumEntityId) return "";
    try {
      return (
        window.localStorage.getItem(
          this._imageSelectionStorageKey(vacuumEntityId)
        ) || ""
      );
    } catch (_err) {
      return "";
    }
  }

  _persistImageEntity(vacuumEntityId, imageEntityId) {
    if (!vacuumEntityId || !imageEntityId) return;
    try {
      window.localStorage.setItem(
        this._imageSelectionStorageKey(vacuumEntityId),
        imageEntityId
      );
    } catch (_err) {
      // Ignore storage failures.
    }
  }

  _sortedImageEntitiesForVacuum(vacuumEntityId) {
    const vacuumObjectId = vacuumEntityId?.split(".", 2)[1] || "";
    return [...this._imageEntities].sort((left, right) => {
      const leftScore = left.includes(vacuumObjectId) ? 0 : 1;
      const rightScore = right.includes(vacuumObjectId) ? 0 : 1;
      if (leftScore !== rightScore) return leftScore - rightScore;
      return left.localeCompare(right);
    });
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

  _unwrapEntityServiceResponse(response) {
    if (!response || typeof response !== "object") return response;
    if (
      this._vacuumEntityId &&
      Object.prototype.hasOwnProperty.call(response, this._vacuumEntityId)
    ) {
      return response[this._vacuumEntityId];
    }
    const keys = Object.keys(response);
    if (keys.length === 1) {
      return response[keys[0]];
    }
    return response;
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
    this._imageEntities = Object.keys(this._hass.states)
      .filter((entityId) => entityId.startsWith("image."))
      .sort();

    if (!this._vacuumEntityId && this._vacuumEntities.length > 0) {
      this._vacuumEntityId = this._vacuumEntities[0];
    }
    if (!this._imageEntityId && this._vacuumEntityId) {
      const persisted = this._loadPersistedImageEntity(this._vacuumEntityId);
      if (persisted && this._imageEntities.includes(persisted)) {
        this._imageEntityId = persisted;
      } else {
        this._imageEntityId =
          this._sortedImageEntitiesForVacuum(this._vacuumEntityId)[0] || "";
      }
    }
    if (this._vacuumEntityId) {
      await this._loadContext();
      return;
    }
    this._render();
  }

  _currentImageUrl() {
    if (!this._hass || !this._context) return null;
    const selectedEntityId = this._imageEntityId || this._context.image_entity_id;
    if (!selectedEntityId) return null;
    const state = this._hass.states[selectedEntityId];
    return state?.attributes?.entity_picture || this._context.image_url || null;
  }

  _backendCanvasWidth() {
    const meta = this._context?.image_meta;
    if (!meta) return 1;
    return (meta.width || 1) * (meta.scale || 1);
  }

  _backendCanvasHeight() {
    const meta = this._context?.image_meta;
    if (!meta) return 1;
    return (meta.height || 1) * (meta.scale || 1);
  }

  _canvasWidth() {
    return this._imageNaturalSize?.width || this._backendCanvasWidth();
  }

  _canvasHeight() {
    return this._imageNaturalSize?.height || this._backendCanvasHeight();
  }

  _backendToDisplay(point) {
    return {
      x: point.x * (this._canvasWidth() / this._backendCanvasWidth()),
      y: point.y * (this._canvasHeight() / this._backendCanvasHeight()),
    };
  }

  _scaledCalibration() {
    const calibration = this._context?.calibration;
    if (!calibration || calibration.length < 3) return null;
    return calibration.map((item) => ({
      vacuum: item.vacuum,
      map: this._backendToDisplay(item.map),
    }));
  }

  _mapToImage(vacuumPoint) {
    const calibration = this._scaledCalibration();
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
    const calibration = this._scaledCalibration();
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
    if (zone.display_rect) {
      const topLeft = this._backendToDisplay({
        x: zone.display_rect.x,
        y: zone.display_rect.y,
      });
      const bottomRight = this._backendToDisplay({
        x: zone.display_rect.x + zone.display_rect.width,
        y: zone.display_rect.y + zone.display_rect.height,
      });
      return {
        x: topLeft.x,
        y: topLeft.y,
        width: bottomRight.x - topLeft.x,
        height: bottomRight.y - topLeft.y,
      };
    }
    const p1 = this._mapToImage({ x: zone.min_x, y: zone.min_y });
    const p2 = this._mapToImage({ x: zone.max_x, y: zone.max_y });
    if (!p1 || !p2) return null;
    return {
      x: Math.min(p1.x, p2.x),
      y: Math.min(p1.y, p2.y),
      width: Math.abs(p2.x - p1.x),
      height: Math.abs(p2.y - p1.y),
    };
  }

  async _loadContext() {
    if (!this._vacuumEntityId) return;
    try {
      const result = await this._callService(
        "roborock_plus",
        "get_safe_zone_editor_context",
        { entity_id: this._vacuumEntityId }
      );
      this._context = this._unwrapEntityServiceResponse(result.response);
      this._draftRect = null;
      if (!this._imageEntityId) {
        const persisted = this._loadPersistedImageEntity(this._vacuumEntityId);
        if (persisted && this._imageEntities.includes(persisted)) {
          this._imageEntityId = persisted;
        } else {
          this._imageEntityId =
            this._sortedImageEntitiesForVacuum(this._vacuumEntityId)[0] || "";
        }
      }
      if (this._vacuumEntityId && this._imageEntityId) {
        this._persistImageEntity(this._vacuumEntityId, this._imageEntityId);
      }
      this._error = this._currentImageUrl()
        ? ""
        : `已加载上下文，但没有找到地图图像。请手动选择 image 实体。候选实体：${
            (this._context?.image_entity_ids || []).join(", ") || "无"
          }`;
      this._notice = this._currentImageUrl() ? "地图已加载。" : "";
    } catch (err) {
      this._error = err?.message || String(err);
      this._notice = "";
    }
    this._render();
  }

  async _saveDraft() {
    if (!this._vacuumEntityId) return;
    const rect = this._draftRect;
    if (!rect) return;
    const p1 = this._imageToMap({ x: rect.x, y: rect.y });
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
    if (this._vacuumEntityId && this._imageEntityId) {
      this._persistImageEntity(this._vacuumEntityId, this._imageEntityId);
    }
    this._error = "";
    this._notice = "安全区已保存。";
    await this._loadContext();
  }

  async _clearZone() {
    if (!this._vacuumEntityId) return;
    await this._hass.callService("roborock_plus", "clear_safe_zone", {
      entity_id: this._vacuumEntityId,
    });
    this._error = "";
    this._notice = "安全区已清空。";
    await this._loadContext();
  }

  _pointerToViewBox(event) {
    const svg = this.shadowRoot.getElementById("editor-svg");
    const rect = svg.getBoundingClientRect();
    const width = this._canvasWidth() || rect.width || 1;
    const height = this._canvasHeight() || rect.height || 1;
    return {
      x: ((event.clientX - rect.left) / rect.width) * width,
      y: ((event.clientY - rect.top) / rect.height) * height,
    };
  }

  _onPointerDown(event) {
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    this._activePointerId = event.pointerId;
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
    if (
      this._activePointerId !== null &&
      event.pointerId !== this._activePointerId
    ) {
      return;
    }
    const point = this._pointerToViewBox(event);
    this._draftRect = {
      x: Math.min(this._dragStart.x, point.x),
      y: Math.min(this._dragStart.y, point.y),
      width: Math.abs(point.x - this._dragStart.x),
      height: Math.abs(point.y - this._dragStart.y),
    };
    this._render();
  }

  _onPointerUp(event) {
    if (
      this._activePointerId !== null &&
      event?.pointerId !== undefined &&
      event.pointerId !== this._activePointerId
    ) {
      return;
    }
    event?.currentTarget?.releasePointerCapture?.(event.pointerId);
    this._dragStart = null;
    this._activePointerId = null;
  }

  _handleImageLoad(event) {
    const img = event.currentTarget;
    const nextSize = {
      width: img.naturalWidth || img.width || 1,
      height: img.naturalHeight || img.height || 1,
    };
    if (
      !this._imageNaturalSize ||
      this._imageNaturalSize.width !== nextSize.width ||
      this._imageNaturalSize.height !== nextSize.height
    ) {
      this._imageNaturalSize = nextSize;
      this._render();
    }
  }

  _render() {
    const imageUrl = this._currentImageUrl();
    const savedRect = this._zoneToDisplayRect(this._context?.safe_zone);
    const currentPos = this._context?.current_position
      ? this._mapToImage(this._context.current_position)
      : null;
    const statusChips = [
      this._notice
        ? `<span class="chip chip-success">${this._notice}</span>`
        : "",
      currentPos
        ? `<span class="chip">当前位置：x=${this._context.current_position.x}, y=${this._context.current_position.y}</span>`
        : "",
      this._context?.safe_zone
        ? `<span class="chip chip-info">安全区：x=${this._context.safe_zone.min_x}..${this._context.safe_zone.max_x}, y=${this._context.safe_zone.min_y}..${this._context.safe_zone.max_y}</span>`
        : "",
      this._error
        ? `<span class="chip chip-danger">${this._error}</span>`
        : "",
    ]
      .filter(Boolean)
      .join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          padding: 12px;
          color: #e7edf6;
        }
        .wrap {
          display: grid;
          gap: 12px;
        }
        .panel {
          display: grid;
          gap: 10px;
          padding: 12px;
          border: 1px solid rgba(148, 163, 184, 0.18);
          border-radius: 14px;
          background:
            linear-gradient(180deg, rgba(39, 47, 58, 0.92), rgba(27, 33, 42, 0.96));
          box-shadow: 0 12px 30px rgba(0, 0, 0, 0.14);
        }
        .toolbar {
          display: grid;
          grid-template-columns: minmax(260px, 380px) minmax(260px, 380px) auto;
          gap: 10px 12px;
          align-items: end;
        }
        .field {
          display: grid;
          gap: 6px;
        }
        .field label {
          font-size: 12px;
          line-height: 1;
          color: #9aa6b2;
          letter-spacing: 0.02em;
          text-transform: uppercase;
        }
        select, input {
          height: 38px;
          padding: 0 12px;
          color: #eef3f8;
          background: rgba(17, 24, 32, 0.72);
          border: 1px solid rgba(148, 163, 184, 0.22);
          border-radius: 10px;
          outline: none;
          box-sizing: border-box;
        }
        select {
          appearance: none;
          -webkit-appearance: none;
          -moz-appearance: none;
          padding-right: 42px;
          background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5L6 6.5L11 1.5' fill='none' stroke='%23b6c2cf' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
          background-repeat: no-repeat;
          background-position: right 14px center;
          background-size: 12px 8px;
        }
        select:focus, input:focus {
          border-color: rgba(96, 165, 250, 0.8);
          box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.16);
        }
        .actions {
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          align-items: stretch;
        }
        button {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          height: 38px;
          padding: 0 14px;
          border: 1px solid rgba(148, 163, 184, 0.22);
          border-radius: 10px;
          background: rgba(17, 24, 32, 0.74);
          color: #eef3f8;
          font-weight: 600;
          line-height: 1;
          box-sizing: border-box;
          white-space: nowrap;
          cursor: pointer;
        }
        button:hover {
          border-color: rgba(148, 163, 184, 0.35);
          background: rgba(30, 41, 59, 0.9);
        }
        .btn-primary {
          background: linear-gradient(180deg, #2b7fff, #1764d8);
          border-color: rgba(96, 165, 250, 0.7);
        }
        .btn-primary:hover {
          background: linear-gradient(180deg, #3a8cff, #1f6fe8);
        }
        .btn-danger {
          color: #fca5a5;
        }
        .helper {
          color: #93a0ad;
          font-size: 13px;
          line-height: 1.4;
        }
        .status-row {
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
        }
        .chip {
          display: inline-flex;
          align-items: center;
          min-height: 28px;
          padding: 0 10px;
          border-radius: 999px;
          border: 1px solid rgba(148, 163, 184, 0.18);
          background: rgba(17, 24, 32, 0.62);
          color: #cdd6e1;
          font-size: 13px;
          line-height: 1;
          white-space: nowrap;
        }
        .chip-success {
          color: #7ee787;
          border-color: rgba(34, 197, 94, 0.28);
          background: rgba(20, 83, 45, 0.22);
        }
        .chip-danger {
          color: #fda4af;
          border-color: rgba(244, 63, 94, 0.26);
          background: rgba(127, 29, 29, 0.26);
          white-space: normal;
        }
        .chip-info {
          color: #9ed0ff;
          border-color: rgba(59, 130, 246, 0.26);
          background: rgba(30, 64, 175, 0.18);
          white-space: normal;
        }
        .editor-card {
          padding: 10px;
          border: 1px solid rgba(148, 163, 184, 0.18);
          border-radius: 14px;
          background: rgba(15, 23, 42, 0.42);
        }
        .editor {
          position: relative;
          display: inline-block;
          width: 100%;
          overflow: auto;
          border-radius: 10px;
          border: 1px solid rgba(148, 163, 184, 0.12);
          background: #1f2630;
        }
        img {
          display: block;
          max-width: 100%;
          height: auto;
        }
        svg {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          cursor: crosshair;
          touch-action: none;
        }
        @media (max-width: 1100px) {
          .toolbar {
            grid-template-columns: 1fr;
          }
        }
      </style>
      <div class="wrap">
        <div class="panel">
          <div class="toolbar">
            <div class="field">
              <label>Vacuum</label>
              <select id="vacuum-select">
              <option value="">请选择</option>
              ${this._vacuumEntities
                .map(
                  (entityId) =>
                    `<option value="${entityId}" ${
                      entityId === this._vacuumEntityId ? "selected" : ""
                    }>${entityId}</option>`
                )
                .join("")}
              </select>
            </div>
            <div class="field">
              <label>Image</label>
              <select id="image-select">
              <option value="">请选择</option>
              ${this._sortedImageEntitiesForVacuum(this._vacuumEntityId)
                .map(
                  (entityId) =>
                    `<option value="${entityId}" ${
                      entityId === this._imageEntityId ? "selected" : ""
                    }>${entityId}</option>`
                )
                .join("")}
              </select>
            </div>
            <div class="actions">
              <button id="load-context">加载地图</button>
              <button id="save-zone" class="btn-primary">保存框选</button>
              <button id="clear-zone" class="btn-danger">清空</button>
            </div>
          </div>
          <div class="helper">先加载地图，再直接拖拽框选。绿色框是当前已保存安全区，橙色框是当前草稿。</div>
          ${statusChips ? `<div class="status-row">${statusChips}</div>` : ""}
        </div>
        ${
          imageUrl
            ? `<div class="editor-card">
                <div class="editor">
                  <img id="map-image" src="${imageUrl}">
                  <svg id="editor-svg" viewBox="0 0 ${this._canvasWidth()} ${this._canvasHeight()}">
                  ${
                    savedRect
                      ? `<rect x="${savedRect.x}" y="${savedRect.y}" width="${savedRect.width}" height="${savedRect.height}" fill="rgba(0,255,0,0.15)" stroke="#00cc66" stroke-width="3"></rect>`
                      : ""
                  }
                  ${
                    this._draftRect
                      ? `<rect x="${this._draftRect.x}" y="${this._draftRect.y}" width="${this._draftRect.width}" height="${this._draftRect.height}" fill="rgba(255,165,0,0.15)" stroke="#ff9900" stroke-width="2"></rect>`
                      : ""
                  }
                  ${
                    currentPos
                      ? `<circle cx="${currentPos.x}" cy="${currentPos.y}" r="8" fill="#ffffff" stroke="#111111" stroke-width="2"></circle>`
                      : ""
                  }
                  </svg>
                </div>
              </div>`
            : `<div class="editor-card"><div class="helper">请选择 vacuum 和 image 实体后再加载地图。</div></div>`
        }
      </div>
    `;

    this.shadowRoot
      .getElementById("vacuum-select")
      ?.addEventListener("change", (ev) => {
        this._vacuumEntityId = ev.target.value;
        const persisted = this._loadPersistedImageEntity(this._vacuumEntityId);
        this._imageEntityId =
          persisted && this._imageEntities.includes(persisted)
            ? persisted
            : this._sortedImageEntitiesForVacuum(this._vacuumEntityId)[0] || "";
        this._context = null;
        this._draftRect = null;
        this._error = "";
        this._notice = "";
        this._render();
      });
    this.shadowRoot
      .getElementById("image-select")
      ?.addEventListener("change", (ev) => {
        this._imageEntityId = ev.target.value;
        if (this._vacuumEntityId && this._imageEntityId) {
          this._persistImageEntity(this._vacuumEntityId, this._imageEntityId);
        }
        this._error = "";
        this._notice = this._currentImageUrl() ? "已切换地图实体。" : "";
        this._render();
      });
    this.shadowRoot
      .getElementById("load-context")
      ?.addEventListener("click", () => this._loadContext());
    this.shadowRoot
      .getElementById("save-zone")
      ?.addEventListener("click", () => this._saveDraft());
    this.shadowRoot
      .getElementById("clear-zone")
      ?.addEventListener("click", () => this._clearZone());

    const img = this.shadowRoot.getElementById("map-image");
    if (img) {
      img.addEventListener("load", (ev) => this._handleImageLoad(ev));
    }
    const svg = this.shadowRoot.getElementById("editor-svg");
    if (svg) {
      svg.addEventListener("pointerdown", (ev) => this._onPointerDown(ev));
      svg.addEventListener("pointermove", (ev) => this._onPointerMove(ev));
      svg.addEventListener("pointerup", (ev) => this._onPointerUp(ev));
      svg.addEventListener("pointerleave", (ev) => this._onPointerUp(ev));
    }
  }
}

if (!customElements.get("roborock-plus-safe-zone-editor")) {
  customElements.define(
    "roborock-plus-safe-zone-editor",
    RoborockPlusSafeZoneEditor
  );
}
