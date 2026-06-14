class BatterySolarOptimiserPlanCard extends HTMLElement {
  setConfig(config) {
    this.config = {
      entity: 'sensor.battery_solar_optimiser_plan',
      title: '24 hour slot plan',
      ...config,
    };
    this.attachShadow({ mode: 'open' });
  }

  set hass(hass) {
    this._hass = hass;
    this.render();
  }

  getCardSize() {
    return 8;
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

  _slotOverrideState(index, slot) {
    const entityId = this._overrideEntity(index);
    const state = this._hass.states[entityId]?.state;
    if (state) return state;
    if (slot.override === 'charge') return 'Force charge';
    if (slot.override === 'discharge') return 'Force discharge';
    return 'No change';
  }

  _changeOverride(index, value) {
    const entityId = this._overrideEntity(index);
    this._hass.callService('select', 'select_option', {
      entity_id: entityId,
      option: value,
    });
  }

  render() {
    if (!this.shadowRoot || !this._hass || !this.config) return;
    const state = this._hass.states[this.config.entity];
    const slots = state?.attributes?.slots || [];
    const rows = slots.map((slot, index) => {
      const override = this._slotOverrideState(index, slot);
      const rowClass = `${slot.is_current ? 'current' : ''} ${this._overrideClass(override)}`.trim();
      const selected = (option) => override === option ? 'selected' : '';
      return `
        <tr class="${rowClass}">
          <td class="time"><strong>${slot.start_local ?? ''}</strong></td>
          <td class="override-cell">
            <select data-slot="${index}" aria-label="Override slot ${index}">
              <option value="No change" ${selected('No change')}>No change</option>
              <option value="Force charge" ${selected('Force charge')}>Force charge</option>
              <option value="Force discharge" ${selected('Force discharge')}>Force discharge</option>
            </select>
          </td>
          <td><span class="badge ${this._statusClass(slot.action)}">${slot.action ?? 'unknown'}</span></td>
          <td>${Number(slot.price ?? 0).toFixed(1)}p</td>
          <td>${Number(slot.battery_percent ?? 0).toFixed(0)}%</td>
          <td>${Number(slot.solar_kwh ?? 0).toFixed(2)}</td>
          <td>£${Number(slot.slot_cost_gbp ?? 0).toFixed(2)}</td>
          <td>£${Number(slot.cumulative_cost_gbp ?? 0).toFixed(2)}</td>
        </tr>`;
    }).join('');

    this.shadowRoot.innerHTML = `
      <ha-card header="${this.config.title}">
        <style>
          :host { display: block; }
          .wrap { overflow-x: auto; padding: 0 12px 12px; }
          table { width: 100%; border-collapse: collapse; font-size: .88rem; min-width: 760px; }
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
          select {
            max-width: 132px;
            min-width: 116px;
            padding: 4px 22px 4px 8px;
            border-radius: 6px;
            border: 1px solid var(--divider-color);
            background: var(--secondary-background-color);
            color: var(--primary-text-color);
            font: inherit;
          }
          tr.forced-charge select { border-color: #10b981; }
          tr.forced-discharge select { border-color: #ef4444; }
          .empty { padding: 16px; color: var(--secondary-text-color); }
        </style>
        ${slots.length ? `
          <div class="wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Override</th>
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

    this.shadowRoot.querySelectorAll('select[data-slot]').forEach((select) => {
      select.addEventListener('change', (event) => {
        this._changeOverride(Number(event.target.dataset.slot), event.target.value);
      });
    });
  }
}

customElements.define('bso-plan-card', BatterySolarOptimiserPlanCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'bso-plan-card',
  name: 'Battery Solar Optimiser Plan Card',
  description: '24 hour battery plan with inline override controls',
});
