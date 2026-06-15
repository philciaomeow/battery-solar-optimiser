class BSOResponsiveStack extends HTMLElement {
  setConfig(config) {
    this.config = config || {};
    this.attachShadow({mode: "open"});
    this._cards = [];
  }
  getCardSize() { return 12; }
  async _createCards(hass) {
    if (this._cards.length || !this.config || !this.config.cards) return;
    const helpers = await window.loadCardHelpers();
    const root = document.createElement("div");
    root.className = "bso-responsive-stack";
    root.innerHTML = `
      <style>
        :host { display: block; }
        .bso-responsive-stack { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        @media (max-width: 768px) {
          .bso-responsive-stack { grid-template-columns: 1fr; }
        }
      </style>`;
    for (const cfg of this.config.cards) {
      const child = await helpers.createCardElement(cfg);
      child.hass = hass;
      this._cards.push(child);
      root.appendChild(child);
    }
    this.shadowRoot.appendChild(root);
  }
  set hass(hass) {
    this._hass = hass;
    this._createCards(hass).then(() => {
      for (const c of this._cards) c.hass = hass;
    });
  }
}
customElements.define("bso-responsive-stack", BSOResponsiveStack);
window.customCards = window.customCards || [];
window.customCards.push({type: "bso-responsive-stack", name: "BSO Responsive 2-column Stack", description: "Stacks children vertically on narrow screens, side by side on desktop."});
