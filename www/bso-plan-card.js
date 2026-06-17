class BatterySolarOptimiserPlanCard extends HTMLElement {
  setConfig(config) {
    this.config = {
      entity: 'sensor.battery_solar_optimiser_plan',
      title: '24 hour slot plan',
      show_overrides: true,
      ...config,
    };
    this.attachShadow({ mode: 'open' });
    this._selectOpen = false;
    this._suppressRenderUntil = 0;
    this._suppressedRenderTimer = null;
    this._pendingOverrides = {};
  }

  set hass(hass) {
    this._hass = hass;
    // Lovelace pushes frequent hass updates. Re-rendering while a native select
    // menu is open closes the dropdown immediately, making overrides almost
    // impossible to choose. Defer the render until interaction finishes.
    if (this._shouldSuppressRender()) {
      this._scheduleRenderAfterSuppression();
      return;
    }
    this.render();
  }

  _escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      "'": '&#39;',
      '"': '&quot;',
    }[char]));
  }

  _markSelectInteraction(ms = 15000, extend = true) {
    this._selectOpen = true;
    const until = Date.now() + ms;
    this._suppressRenderUntil = extend ? Math.max(this._suppressRenderUntil || 0, until) : until;
  }

  _scheduleRenderAfterSuppression() {
    if (this._suppressedRenderTimer) clearTimeout(this._suppressedRenderTimer);
    const delay = Math.max(50, (this._suppressRenderUntil || 0) - Date.now() + 50);
    this._suppressedRenderTimer = setTimeout(() => {
      this._suppressedRenderTimer = null;
      this._selectOpen = false;
      if (!this._shouldSuppressRender()) this.render();
    }, delay);
  }

  _shouldSuppressRender() {
    if (Date.now() < (this._suppressRenderUntil || 0)) return true;
    if (!this.shadowRoot) return false;
    const active = this.shadowRoot.activeElement;
    if (active && active.tagName === 'SELECT') return true;
    return this._selectOpen;
  }

  getCardSize() {
    return 12;
  }

  _overrideEntity(index) {
    return `select.battery_solar_optimiser_slot_${String(index).padStart(2, '0')}_override`;
  }

  _statusClass(action) {
    if (action === 'charging') return 'charge';
    if (action === 'discharging') return 'discharge';
    return 'hold';
  }

  _overrideClass(value) {
    if (value === 'Force charge') return 'forced-charge';
    if (value === 'Force discharge') return 'forced-discharge';
    return '';
  }

  _overrideLabel(value) {
    if (value === 'Force charge') return 'Charge';
    if (value === 'Force discharge') return 'Discharge';
    return 'Off';
  }

  _slotOverrideState(index, slot) {
    if (Object.prototype.hasOwnProperty.call(this._pendingOverrides || {}, index)) {
      return this._pendingOverrides[index];
    }
    const entityId = this._overrideEntity(index);
    const state = this._hass.states[entityId]?.state;
    if (state) return state;
    if (slot.override === 'charge') return 'Force charge';
    if (slot.override === 'discharge') return 'Force discharge';
    return 'No change';
  }

  _changeOverride(index, value) {
    const entityId = this._overrideEntity(index);
    this._pendingOverrides[index] = value;
    this.render();
    this._hass.callService('select', 'select_option', {
      entity_id: entityId,
      option: value,
    }).catch(() => {
      delete this._pendingOverrides[index];
      this.render();
    });
    setTimeout(() => {
      const current = this._hass?.states?.[entityId]?.state;
      if (!current || current === value) {
        delete this._pendingOverrides[index];
        this.render();
      }
    }, 1500);
  }

  render() {
    if (!this.shadowRoot || !this._hass || !this.config) return;
    const state = this._hass.states[this.config.entity];
    const slots = state?.attributes?.slots || [];
    const rows = slots.map((slot, index) => {
      const override = this._slotOverrideState(index, slot);
      const rowClass = `${slot.is_current ? 'current' : ''} ${this._overrideClass(override)}`.trim();
      const escapedTime = this._escapeHtml(slot.start_local ?? '');
      const escapedAction = this._escapeHtml(slot.action ?? 'unknown');
      const escapedOverride = this._escapeHtml(this._overrideLabel(override));
      const chargeActive = override === 'Force charge';
      const dischargeActive = override === 'Force discharge';
      const overrideCell = this.config.show_overrides ? `
          <td class="override-cell">
            <div class="override-buttons" role="group" aria-label="Override slot ${index}">
              <button
                class="override-button charge ${chargeActive ? 'active' : ''}"
                data-slot="${index}"
                data-value="Force charge"
                aria-pressed="${chargeActive ? 'true' : 'false'}"
              >Charge</button>
              <button
                class="override-button discharge ${dischargeActive ? 'active' : ''}"
                data-slot="${index}"
                data-value="Force discharge"
                aria-pressed="${dischargeActive ? 'true' : 'false'}"
              >Discharge</button>
            </div>
          </td>` : `<td class="override-badge"><span class="badge ${this._overrideClass(override) || 'override-off'}">${escapedOverride}</span></td>`;
      return `
        <tr class="${rowClass}">
          <td class="time"><strong>${escapedTime}</strong></td>
          ${overrideCell}
          <td><span class="badge ${this._statusClass(slot.action)}">${escapedAction}</span></td>
          <td>${Number(slot.price ?? 0).toFixed(1)}p</td>
          <td>${Number(slot.battery_percent ?? 0).toFixed(0)}%</td>
          <td>${Number(slot.solar_kwh ?? 0).toFixed(2)}</td>
          <td>£${Number(slot.slot_cost_gbp ?? 0).toFixed(2)}</td>
          <td>£${Number(slot.cumulative_cost_gbp ?? 0).toFixed(2)}</td>
        </tr>`;
    }).join('');

    this.shadowRoot.innerHTML = `
      <ha-card header="${this._escapeHtml(this.config.title)}">
        <style>
          :host { display: block; }
          .wrap { overflow-x: auto; padding: 0 12px 12px; }
          table { width: 100%; border-collapse: collapse; font-size: .88rem; min-width: 900px; }
          th { position: sticky; top: 0; background: var(--card-background-color); z-index: 1; color: var(--secondary-text-color); font-weight: 700; }
          td, th { padding: 6px 8px; border-bottom: 1px solid var(--divider-color); text-align: right; white-space: nowrap; }
          td:first-child, th:first-child, td:nth-child(2), th:nth-child(2), td:nth-child(3), th:nth-child(3) { text-align: left; }
          tr.current { background: rgba(96,165,250,.16); }
          tr.forced-charge { background: rgba(16,185,129,.20); }
          tr.forced-discharge { background: rgba(239,68,68,.20); }
          .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-weight: 700; }
          .badge.charge { background: #064e3b; color: #34d399; }
          .badge.discharge { background: #7f1d1d; color: #f87171; }
          .badge.hold { background: #374151; color: #d1d5db; }
          .badge.forced-charge { background: #065f46; color: #6ee7b7; }
          .badge.forced-discharge { background: #991b1b; color: #fecaca; }
          .override-buttons { display: inline-flex; gap: 6px; align-items: center; }
          .override-button {
            appearance: none;
            border: 1px solid var(--divider-color);
            border-radius: 999px;
            background: var(--secondary-background-color);
            color: var(--primary-text-color);
            font: inherit;
            font-weight: 700;
            padding: 5px 10px;
            cursor: pointer;
          }
          .override-button.charge.active { background: #065f46; border-color: #10b981; color: #6ee7b7; }
          .override-button.discharge.active { background: #991b1b; border-color: #ef4444; color: #fecaca; }
          .override-button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
          .badge.override-off { background: #374151; color: #d1d5db; }
          .override-badge { text-align: left; }
          .empty { padding: 16px; color: var(--secondary-text-color); }
        </style>
        ${slots.length ? `
          <div class="wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>${this.config.show_overrides ? 'Override' : 'Override status'}</th>
                  <th>Status</th>
                  <th>Agile</th>
                  <th>Bat %</th>
                  <th>Solar kWh</th>
                  <th>Slot cost</th>
                  <th>Daily cost</th>
                </tr>
              </thead>
              <tbody>${rows}</tbody>
            </table>
          </div>` : '<div class="empty">No plan data yet. Press Recalculate now or wait for the next refresh.</div>'}
      </ha-card>
    `;

    if (this.config.show_overrides) {
      this.shadowRoot.querySelectorAll('button[data-slot]').forEach((button) => {
        button.addEventListener('click', (event) => {
          const slot = Number(event.currentTarget.dataset.slot);
          const requested = event.currentTarget.dataset.value;
          const current = this._slotOverrideState(slot, slots[slot]);
          const next = current === requested ? 'No change' : requested;
          this._changeOverride(slot, next);
        });
      });
    }
  }
}

if (!customElements.get('bso-plan-card')) customElements.define('bso-plan-card', BatterySolarOptimiserPlanCard);
window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === 'bso-plan-card')) {
  window.customCards.push({
    type: 'bso-plan-card',
    name: 'Battery Solar Optimiser Plan Card',
    description: '24 hour battery plan with inline override controls',
  });
}
