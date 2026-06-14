class BatterySolarOptimiserLayoutCard extends HTMLElement {
  setConfig(config) {
    this.config = config || {};
    this.attachShadow({ mode: 'open' });
    this._cards = [];
  }

  getCardSize() {
    return 12;
  }

  async _createCards(hass) {
    if (this._cards.length || !this.config) return;
    const helpers = await window.loadCardHelpers();
    const make = async (config, cls) => {
      const card = await helpers.createCardElement(config);
      card.hass = hass;
      const wrap = document.createElement('div');
      wrap.className = cls;
      wrap.appendChild(card);
      this._cards.push(card);
      return wrap;
    };

    const root = document.createElement('div');
    root.className = 'bso-dashboard-layout';
    root.appendChild(await make(this.config.header, 'header'));

    const main = document.createElement('div');
    main.className = 'main-grid';

    const graphs = document.createElement('div');
    graphs.className = 'graphs stack';
    for (const cfg of this.config.graphs || []) {
      graphs.appendChild(await make(cfg, 'graph-card'));
    }

    const plan = await make(this.config.plan, 'plan');

    const side = document.createElement('div');
    side.className = 'side stack';
    side.appendChild(await make(this.config.status, 'status'));
    side.appendChild(await make(this.config.totals, 'totals'));
    if (this.config.history) side.appendChild(await make(this.config.history, 'history'));

    main.appendChild(graphs);
    main.appendChild(plan);
    main.appendChild(side);
    root.appendChild(main);

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        .bso-dashboard-layout { display: flex; flex-direction: column; gap: 12px; }
        .main-grid {
          display: grid;
          grid-template-columns: minmax(320px, 0.9fr) minmax(620px, 1.55fr) minmax(260px, 0.65fr);
          gap: 12px;
          align-items: start;
        }
        .stack { display: flex; flex-direction: column; gap: 12px; }
        .header, .graphs, .plan, .side { min-width: 0; }
        @media (max-width: 1500px) {
          .main-grid { grid-template-columns: minmax(560px, 1.5fr) minmax(260px, 0.7fr); }
          .graphs { grid-column: 1 / 2; }
          .plan { grid-column: 1 / 2; }
          .side { grid-column: 2 / 3; grid-row: 1 / span 2; }
        }
        @media (max-width: 980px) {
          .main-grid { display: flex; flex-direction: column; }
        }
      </style>
    `;
    this.shadowRoot.appendChild(root);
  }

  set hass(hass) {
    this._hass = hass;
    this._createCards(hass).then(() => {
      for (const card of this._cards) card.hass = hass;
    });
  }
}

customElements.define('bso-layout-card', BatterySolarOptimiserLayoutCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'bso-layout-card',
  name: 'Battery Solar Optimiser Layout Card',
  description: 'Responsive layout wrapper for the Battery Solar Optimiser dashboard',
});
