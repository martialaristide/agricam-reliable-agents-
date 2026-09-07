/* Interface de vérification AgriCam Reliable Agents.
   Sans framework, sans étape de build : un routeur par fragment d'URL,
   des vues qui rendent du HTML, des figures SVG dessinées à la main.
   Plan de design : docs/interface/plan-de-design.md. */

(function () {
  "use strict";

  // ---- Utilitaires --------------------------------------------------------

  const fmtNombre2 = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtNombre3 = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  const fmtPct = new Intl.NumberFormat("fr-FR", { style: "percent", maximumFractionDigits: 0 });
  const fmtPct1 = new Intl.NumberFormat("fr-FR", { style: "percent", maximumFractionDigits: 1 });
  const fmtUsd = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  const fmtMs = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });
  const fmtDate = new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });

  function h(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  const n2 = (v) => (v == null ? "–" : fmtNombre2.format(v));
  const n3 = (v) => (v == null ? "–" : fmtNombre3.format(v));
  const pct = (v) => (v == null ? "–" : fmtPct.format(v));
  const pct1 = (v) => (v == null ? "–" : fmtPct1.format(v));
  const usd = (v) => (v == null ? "–" : fmtUsd.format(v) + " $");
  const ms = (v) => (v == null ? "–" : fmtMs.format(v) + " ms");
  const date = (iso) => (iso ? fmtDate.format(new Date(iso)) : "–");
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const json = (v) => h(JSON.stringify(v));

  async function api(path, options) {
    const response = await fetch(path, options);
    let body = null;
    try { body = await response.json(); } catch (_) { body = null; }
    if (!response.ok) {
      const message = body && body.error ? body.error : "Le serveur n'a pas répondu (" + response.status + ").";
      throw new Error(message);
    }
    return body;
  }

  function lienEssai(campaignId, taskId, trialId) {
    return "#/campagne/" + campaignId + "/essai/" + encodeURIComponent(taskId) + "/" + trialId;
  }
  function lienTache(campaignId, taskId) {
    return "#/campagne/" + campaignId + "/tache/" + encodeURIComponent(taskId);
  }

  const ETAT = {
    verifie: "vérifié", echec: "échec avoué", surconfiance: "sur-confiance",
  };

  function etat(state, label) {
    return '<span class="etat etat-' + h(state) + '">' + h(label || ETAT[state] || state) + "</span>";
  }

  // ---- Figures SVG ----------------------------------------------------------

  /* Règle graduée : p̂ (aiguille) et son intervalle de Wilson (barre à butées). */
  function regleWilson(prop, options) {
    const o = Object.assign({ largeur: 560, mini: false }, options || {});
    if (!prop) {
      return o.mini ? '<span class="note">aucun essai</span>' : '<p class="note">Aucun essai : rien à mesurer.</p>';
    }
    const W = o.largeur, x0 = o.mini ? 2 : 16, x1 = W - (o.mini ? 2 : 16);
    const X = (v) => x0 + (x1 - x0) * clamp(v, 0, 1);
    if (o.mini) {
      const y = 8;
      return '<svg class="mini" viewBox="0 0 160 16" role="img" aria-label="p̂ ' + n2(prop.p_hat) + ', intervalle de ' + n2(prop.ci_low) + ' à ' + n2(prop.ci_high) + '">' +
        '<line x1="' + x0 + '" y1="' + y + '" x2="' + x1 + '" y2="' + y + '" stroke="#55635A" stroke-width="1"/>' +
        [0, 0.25, 0.5, 0.75, 1].map((t) => '<line x1="' + X(t) + '" y1="' + (y - 3) + '" x2="' + X(t) + '" y2="' + (y + 3) + '" stroke="#55635A" stroke-width="1"/>').join("") +
        '<line x1="' + X(prop.ci_low) + '" y1="' + y + '" x2="' + X(prop.ci_high) + '" y2="' + y + '" stroke="#14231C" stroke-width="5" stroke-opacity=".35"/>' +
        '<line x1="' + X(prop.p_hat) + '" y1="1" x2="' + X(prop.p_hat) + '" y2="15" stroke="#14231C" stroke-width="2"/>' +
        "</svg>";
    }
    const yAxe = 44;
    const ticks = [];
    for (let t = 0; t <= 1.0001; t += 0.05) {
      const grand = Math.round(t * 100) % 25 === 0;
      ticks.push('<line x1="' + X(t) + '" y1="' + yAxe + '" x2="' + X(t) + '" y2="' + (yAxe + (grand ? 10 : 5)) + '" stroke="#55635A" stroke-width="1"/>');
      if (grand) ticks.push('<text x="' + X(t) + '" y="' + (yAxe + 24) + '" text-anchor="middle">' + pct(t) + "</text>");
    }
    const xLo = X(prop.ci_low), xHi = X(prop.ci_high), xP = X(prop.p_hat);
    const libelle = "p̂ " + n2(prop.p_hat);
    const xTexte = clamp(xP, x0 + 30, x1 - 30);
    const title = "Proportion de succès vérifiés " + n2(prop.p_hat) + ", intervalle de confiance à 95 % de " + n2(prop.ci_low) + " à " + n2(prop.ci_high) + ", sur " + prop.n + " essais.";
    return '<svg viewBox="0 0 ' + W + ' 96" role="img" aria-label="' + h(title) + '"><title>' + h(title) + "</title>" +
      '<line x1="' + x0 + '" y1="' + yAxe + '" x2="' + x1 + '" y2="' + yAxe + '" stroke="#14231C" stroke-width="1"/>' +
      ticks.join("") +
      '<line x1="' + xLo + '" y1="' + yAxe + '" x2="' + xHi + '" y2="' + yAxe + '" stroke="#14231C" stroke-width="8" stroke-opacity=".28"/>' +
      '<line x1="' + xLo + '" y1="' + (yAxe - 9) + '" x2="' + xLo + '" y2="' + (yAxe + 9) + '" stroke="#14231C" stroke-width="1.5"/>' +
      '<line x1="' + xHi + '" y1="' + (yAxe - 9) + '" x2="' + xHi + '" y2="' + (yAxe + 9) + '" stroke="#14231C" stroke-width="1.5"/>' +
      '<line x1="' + xP + '" y1="' + (yAxe - 22) + '" x2="' + xP + '" y2="' + (yAxe + 12) + '" stroke="#14231C" stroke-width="2"/>' +
      '<polygon points="' + (xP - 5) + "," + (yAxe - 28) + " " + (xP + 5) + "," + (yAxe - 28) + " " + xP + "," + (yAxe - 20) + '" fill="#14231C"/>' +
      '<text class="valeur" x="' + xTexte + '" y="' + (yAxe - 32) + '" text-anchor="middle">' + h(libelle) + "</text>" +
      '<text x="' + clamp(xLo, x0 + 20, x1 - 20) + '" y="' + (yAxe + 24) + '" text-anchor="middle" dy="14">' + n2(prop.ci_low) + "</text>" +
      '<text x="' + clamp(xHi, x0 + 20, x1 - 20) + '" y="' + (yAxe + 24) + '" text-anchor="middle" dy="14">' + n2(prop.ci_high) + "</text>" +
      "</svg>";
  }

  /* Courbe pass^k avec la limite de décision dessinée et la zone hors contrôle hachurée. */
  function courbePassK(curve, seuil) {
    const W = 560, H = 250, l = 48, r = 20, t = 18, b = 40;
    const K = curve.length;
    const X = (k) => l + ((W - l - r) * (k - 1)) / Math.max(1, K - 1);
    const Y = (v) => t + (H - t - b) * (1 - v);
    let kRupture = null;
    for (const p of curve) { if (p.value < seuil) { kRupture = p.k; break; } }
    const grille = [0, 0.25, 0.5, 0.75, 1].map((v) =>
      '<line x1="' + l + '" y1="' + Y(v) + '" x2="' + (W - r) + '" y2="' + Y(v) + '" stroke="#55635A" stroke-opacity=".35" stroke-width="1"/>' +
      '<text x="' + (l - 8) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + pct(v) + "</text>").join("");
    const axeX = curve.map((p) => '<text x="' + X(p.k) + '" y="' + (H - b + 18) + '" text-anchor="middle">' + p.k + "</text>").join("");
    const chemin = curve.map((p, i) => (i ? "L" : "M") + X(p.k) + "," + Y(p.value)).join(" ");
    // Bande d'incertitude : image de l'intervalle de Wilson par x ↦ x^k.
    const avecBande = curve.every((p) => p.low != null && p.high != null);
    const bande = avecBande
      ? '<path d="' + curve.map((p, i) => (i ? "L" : "M") + X(p.k) + "," + Y(p.high)).join(" ") +
        " " + curve.slice().reverse().map((p) => "L" + X(p.k) + "," + Y(p.low)).join(" ") + ' Z" fill="#14231C" fill-opacity=".10" stroke="none"/>'
      : "";
    const points = curve.map((p) => {
      const sous = p.value < seuil;
      const detail = p.low != null ? " [" + n3(p.low) + " ; " + n3(p.high) + "]" : "";
      return '<circle cx="' + X(p.k) + '" cy="' + Y(p.value) + '" r="4.5" fill="' + (sous ? "#B3261E" : "#F7F9F6") + '" stroke="' + (sous ? "#B3261E" : "#14231C") + '" stroke-width="2"><title>k = ' + p.k + " : pass^k = " + n3(p.value) + detail + "</title></circle>";
    }).join("");
    const zone = '<rect x="' + l + '" y="' + Y(seuil) + '" width="' + (W - l - r) + '" height="' + (Y(0) - Y(seuil)) + '" fill="url(#hachures)"/>';
    const limite = '<line x1="' + l + '" y1="' + Y(seuil) + '" x2="' + (W - r) + '" y2="' + Y(seuil) + '" stroke="#14231C" stroke-width="1.5" stroke-dasharray="6 5"/>' +
      '<text class="gravee" x="' + (W - r) + '" y="' + (Y(seuil) - 6) + '" text-anchor="end">seuil de décision ' + pct(seuil) + "</text>";
    const rupture = kRupture == null
      ? '<text class="gravee" x="' + l + '" y="' + (t - 4) + '">tient jusqu\'à k = ' + K + "</text>"
      : '<text class="alerte" x="' + l + '" y="' + (t - 4) + '">hors contrôle à partir de k = ' + kRupture + "</text>";
    const title = "Courbe pass^k : " + (kRupture == null ? "reste au-dessus du seuil de " + pct(seuil) + " jusqu'à k = " + K : "passe sous le seuil de " + pct(seuil) + " à partir de k = " + kRupture) + ".";
    return '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + h(title) + '"><title>' + h(title) + "</title>" +
      '<defs><pattern id="hachures" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="8" stroke="#B3261E" stroke-opacity=".28" stroke-width="1.5"/></pattern></defs>' +
      zone + grille + bande + limite +
      '<line x1="' + l + '" y1="' + t + '" x2="' + l + '" y2="' + (H - b) + '" stroke="#14231C" stroke-width="1"/>' +
      '<line x1="' + l + '" y1="' + (H - b) + '" x2="' + (W - r) + '" y2="' + (H - b) + '" stroke="#14231C" stroke-width="1"/>' +
      axeX + '<text x="' + (W - r) + '" y="' + (H - 4) + '" text-anchor="end">k succès consécutifs exigés</text>' +
      '<path d="' + chemin + '" fill="none" stroke="#14231C" stroke-width="2"/>' + points + rupture +
      "</svg>";
  }

  /* Carte de contrôle des essais : une marque par essai (forme = état) et l'estimation courante de p̂. */
  function carteControle(trials, campaignId, taskId) {
    const W = 560, H = 150, l = 48, r = 16, yMarques = 22, yHaut = 46, yBas = 130;
    const n = trials.length;
    const X = (i) => l + ((W - l - r) * (n === 1 ? 0.5 : i / (n - 1)));
    const Y = (v) => yHaut + (yBas - yHaut) * (1 - v);
    let cumul = 0;
    const courante = trials.map((tr, i) => { cumul += tr.state === "verifie" ? 1 : 0; return cumul / (i + 1); });
    const chemin = courante.map((v, i) => (i ? "L" : "M") + X(i) + "," + Y(v)).join(" ");
    const grille = [0, 0.5, 1].map((v) =>
      '<line x1="' + l + '" y1="' + Y(v) + '" x2="' + (W - r) + '" y2="' + Y(v) + '" stroke="#55635A" stroke-opacity=".35"/>' +
      '<text x="' + (l - 8) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + pct(v) + "</text>").join("");
    const marques = trials.map((tr, i) => {
      const x = X(i);
      const titre = "Essai " + tr.trial_id + " : " + ETAT[tr.state];
      let forme;
      if (tr.state === "verifie") forme = '<circle class="marque-essai" cx="' + x + '" cy="' + yMarques + '" r="5" fill="#1F6F4A"/>';
      else if (tr.state === "surconfiance") forme = '<rect class="marque-essai" x="' + (x - 5) + '" y="' + (yMarques - 5) + '" width="10" height="10" fill="#B3261E"/>';
      else forme = '<circle class="marque-essai" cx="' + x + '" cy="' + yMarques + '" r="5" fill="#F7F9F6" stroke="#14231C" stroke-width="1.5"/>';
      return '<a href="' + lienEssai(campaignId, taskId, tr.trial_id) + '" aria-label="' + h(titre) + '"><title>' + h(titre) + "</title>" +
        '<rect x="' + (x - 7) + '" y="' + (yMarques - 9) + '" width="14" height="18" fill="transparent"/>' + forme + "</a>";
    }).join("");
    const title = "Carte de contrôle : " + n + " essais, dans l'ordre d'exécution ; l'estimation courante de p̂ finit à " + n2(courante[n - 1]) + ".";
    return '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + h(title) + '"><title>' + h(title) + "</title>" +
      '<text class="gravee" x="' + (l - 8) + '" y="' + (yMarques + 4) + '" text-anchor="end">essais</text>' +
      marques + grille +
      '<text class="gravee" x="' + l + '" y="' + (yHaut - 8) + '">estimation courante de p̂</text>' +
      '<path d="' + chemin + '" fill="none" stroke="#14231C" stroke-width="1.5"/>' +
      '<circle cx="' + X(n - 1) + '" cy="' + Y(courante[n - 1]) + '" r="3.5" fill="#14231C"/>' +
      "</svg>";
  }

  /* Barres horizontales fines, une teinte, valeur écrite après la barre.
     La plus longue barre n'occupe que 60 % de la largeur disponible (la
     valeur reste vraie, l'échelle garde un zéro) : sans cette marge, sa
     propre étiquette ("0,0194 $ (n = 30)") déborderait du cadre SVG. */
  function barres(rows, cle, format, options) {
    const o = Object.assign({ largeurLibelle: 170 }, options || {});
    const W = 560, l = o.largeurLibelle, r = 24, h0 = 12, pas = 26;
    const H = h0 + rows.length * pas + 6;
    const max = Math.max(1e-12, ...rows.map((row) => row[cle] || 0));
    const domaine = max / 0.6;
    const X = (v) => l + ((W - l - r) * v) / domaine;
    const body = rows.map((row, i) => {
      const y = h0 + i * pas;
      return '<text x="' + (l - 10) + '" y="' + (y + 13) + '" text-anchor="end" class="valeur">' + h(row.task_id) + "</text>" +
        '<rect x="' + l + '" y="' + (y + 4) + '" width="' + Math.max(1, X(row[cle]) - l) + '" height="12" fill="#14231C"/>' +
        '<text x="' + (X(row[cle]) + 8) + '" y="' + (y + 13) + '">' + h(format(row[cle])) + " (n = " + row.n + ")</text>";
    }).join("");
    return '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + h(o.titre || "") + '"><title>' + h(o.titre || "") + "</title>" +
      '<line x1="' + l + '" y1="' + (h0 - 2) + '" x2="' + l + '" y2="' + (H - 4) + '" stroke="#14231C"/>' + body + "</svg>";
  }

  /* Courbe dans le temps (latence par essai, dans l'ordre d'exécution). */
  function courbeTemps(points, cle, format, titre) {
    const W = 560, H = 170, l = 60, r = 16, t = 16, b = 34;
    const n = points.length;
    const max = Math.max(1e-12, ...points.map((p) => p[cle] || 0));
    const X = (i) => l + ((W - l - r) * (n === 1 ? 0.5 : i / (n - 1)));
    const Y = (v) => t + (H - t - b) * (1 - v / max);
    const moyenne = points.reduce((s, p) => s + (p[cle] || 0), 0) / Math.max(1, n);
    const chemin = points.map((p, i) => (i ? "L" : "M") + X(i) + "," + Y(p[cle] || 0)).join(" ");
    const grille = [0, 0.5, 1].map((f) =>
      '<line x1="' + l + '" y1="' + Y(max * f) + '" x2="' + (W - r) + '" y2="' + Y(max * f) + '" stroke="#55635A" stroke-opacity=".35"/>' +
      '<text x="' + (l - 8) + '" y="' + (Y(max * f) + 4) + '" text-anchor="end">' + h(format(max * f)) + "</text>").join("");
    // Étiquette « moyenne » : dessinée APRÈS le tracé (donc au-dessus), avec un
    // fond opaque approximatif, sinon la ligne en dents de scie la traverse.
    const libelleMoyenne = "moyenne " + format(moyenne);
    const largeurMoyenne = libelleMoyenne.length * 6.4 + 6;
    return '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + h(titre) + '"><title>' + h(titre) + "</title>" + grille +
      '<line x1="' + l + '" y1="' + Y(moyenne) + '" x2="' + (W - r) + '" y2="' + Y(moyenne) + '" stroke="#14231C" stroke-dasharray="6 5" stroke-width="1.5"/>' +
      '<path d="' + chemin + '" fill="none" stroke="#14231C" stroke-width="1.5"/>' +
      '<rect x="' + (W - r - largeurMoyenne) + '" y="' + (Y(moyenne) - 17) + '" width="' + largeurMoyenne + '" height="14" fill="#F7F9F6" fill-opacity=".92"/>' +
      '<text class="gravee" x="' + (W - r) + '" y="' + (Y(moyenne) - 6) + '" text-anchor="end">' + h(libelleMoyenne) + "</text>" +
      '<line x1="' + l + '" y1="' + (H - b) + '" x2="' + (W - r) + '" y2="' + (H - b) + '" stroke="#14231C"/>' +
      '<text x="' + l + '" y="' + (H - b + 18) + '">1er essai</text><text x="' + (W - r) + '" y="' + (H - b + 18) + '" text-anchor="end">' + n + "e essai</text>" +
      "</svg>";
  }

  /* Haltères avant → après par tâche ; les régressions en alerte. */
  function halteres(rows) {
    const W = 560, l = 180, r = 24, h0 = 30, pas = 30;
    const H = h0 + rows.length * pas + 10;
    const X = (v) => l + (W - l - r) * clamp(v, 0, 1);
    const axe = [0, 0.25, 0.5, 0.75, 1].map((v) =>
      '<line x1="' + X(v) + '" y1="' + (h0 - 12) + '" x2="' + X(v) + '" y2="' + (H - 8) + '" stroke="#55635A" stroke-opacity=".35"/>' +
      '<text x="' + X(v) + '" y="' + (h0 - 16) + '" text-anchor="middle">' + pct(v) + "</text>").join("");
    const body = rows.map((row, i) => {
      const y = h0 + i * pas + 8;
      const alerte = row.verdict === "regression";
      const couleur = alerte ? "#B3261E" : "#14231C";
      let figure = "";
      if (row.before && row.after) {
        figure = '<line x1="' + X(row.after.ci_low) + '" y1="' + y + '" x2="' + X(row.after.ci_high) + '" y2="' + y + '" stroke="' + couleur + '" stroke-width="6" stroke-opacity=".22"/>' +
          '<line x1="' + X(row.before.p_hat) + '" y1="' + y + '" x2="' + X(row.after.p_hat) + '" y2="' + y + '" stroke="' + couleur + '" stroke-width="2"/>' +
          '<circle cx="' + X(row.before.p_hat) + '" cy="' + y + '" r="5" fill="#F7F9F6" stroke="' + couleur + '" stroke-width="2"/>' +
          '<circle cx="' + X(row.after.p_hat) + '" cy="' + y + '" r="5" fill="' + couleur + '"/>';
      } else if (row.before) {
        figure = '<circle cx="' + X(row.before.p_hat) + '" cy="' + y + '" r="5" fill="#F7F9F6" stroke="#55635A" stroke-width="2"/>';
      } else if (row.after) {
        figure = '<circle cx="' + X(row.after.p_hat) + '" cy="' + y + '" r="5" fill="#55635A"/>';
      }
      return '<text class="' + (alerte ? "alerte" : "valeur") + '" x="' + (l - 12) + '" y="' + (y + 4) + '" text-anchor="end">' + h(row.task_id) + "</text>" + figure;
    }).join("");
    const title = "Comparaison avant/après : " + rows.length + " tâches, " + rows.filter((r0) => r0.verdict === "regression").length + " en régression.";
    return '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + h(title) + '"><title>' + h(title) + "</title>" + axe + body + "</svg>";
  }

  // ---- Règle (navigation) ------------------------------------------------------

  const state = { campaigns: [], campaignId: null, route: "" };

  function renduRegle() {
    const c = state.campaigns.find((x) => x.id === state.campaignId) || null;
    const courante = c
      ? '<div class="courante">Campagne ouverte<b>' + h("n° " + c.id + " " + c.name) + "</b></div>"
      : '<div class="courante">Aucune campagne ouverte<b>Choisissez-en une</b></div>';
    const canal = (href, libelle, abbr, actif) =>
      '<li><a class="canal" href="' + href + '"' + (actif ? ' aria-current="page"' : "") + '><span class="trait"></span><span>' + h(libelle) + '</span><abbr title="' + h(libelle) + '">' + h(abbr) + "</abbr></a></li>";
    const r = state.route;
    const id = state.campaignId;
    const canaux = [
      canal("#/campagnes", "Campagnes", "C", r === "campagnes"),
      id ? canal("#/campagne/" + id, "Tâches", "T", r === "campagne" || r === "tache" || r === "essai") : "",
      id ? canal("#/campagne/" + id + "/securite", "Sécurité", "S", r === "securite") : "",
      id ? canal("#/campagne/" + id + "/cout", "Coût et latence", "€", r === "cout") : "",
      canal("#/comparer", "Comparer", "≷", r === "comparer"),
      canal("#/connecter", "Connecter un agent", "⚭", r === "connecter"),
    ].join("");
    document.getElementById("regle").innerHTML =
      '<p class="marque">AgriCam<br>Reliable Agents<small>Interface de vérification</small></p>' +
      courante + "<ul>" + canaux + "</ul>" +
      '<div class="separateur"></div><p class="pied">Chaque proportion est dessinée avec son intervalle de confiance à 95 % (Wilson). Un chiffre sans intervalle n\'est pas une mesure.</p>';
  }

  // ---- Vues ---------------------------------------------------------------------

  function enTeteCampagne(c, titre, sousTitre) {
    return '<p class="fil"><a href="#/campagnes">Campagnes</a> › <a href="#/campagne/' + c.id + '">n° ' + c.id + " " + h(c.name) + "</a></p>" +
      "<h1>" + h(titre) + "</h1>" + (sousTitre ? '<p class="sous-titre">' + sousTitre + "</p>" : "");
  }

  function mesure(valeur, etiquette, incertitude, classe) {
    return '<li class="mesure' + (classe ? " " + classe : "") + '"><span class="etiquette">' + h(etiquette) + "</span><b>" + valeur + "</b>" +
      (incertitude ? '<span class="incertitude">' + incertitude + "</span>" : "") + "</li>";
  }

  function bandeauCampagne(c) {
    const p = c.proportion;
    return '<ul class="bandeau">' +
      mesure(h(c.n_success + " / " + c.n_trials), "essais vérifiés", p ? "IC 95 % [" + n2(p.ci_low) + " ; " + n2(p.ci_high) + "]" : "aucun essai") +
      mesure(p ? h(n2(p.p_hat)) : "–", "p̂ toutes tâches", p ? "sur " + c.n_trials + " essais, " + c.n_tasks + " tâches" : "") +
      mesure(h(String(c.n_overconfident)), "sur-confiance", c.n_trials ? pct1(c.n_overconfident / c.n_trials) + " des essais" : "", c.n_overconfident ? "mesure-alerte" : "") +
      mesure(h(String(c.incidents.unblocked)), "incidents non bloqués", c.incidents.total + " incidents, " + c.incidents.blocked + " bloqués", c.incidents.unblocked ? "mesure-alerte" : "mesure-verifie") +
      mesure(h(usd(c.total_cost_usd)), "coût estimé", c.mean_latency_ms != null ? "latence moyenne " + ms(c.mean_latency_ms) : "") +
      "</ul>";
  }

  async function vueCampagnes() {
    const data = await api("/api/campaigns");
    state.campaigns = data.campaigns;
    const lignes = data.campaigns.map((c) => {
      const p = c.proportion;
      const alerte = c.incidents.unblocked > 0 || c.status === "échouée";
      return '<tr' + (alerte ? ' class="ligne-alerte"' : "") + '><td class="num">' + c.id + '</td>' +
        '<td><a class="lien-ligne" href="#/campagne/' + c.id + '">' + h(c.name) + "</a>" + (c.model ? '<br><span class="note">' + h(c.model) + "</span>" : "") + "</td>" +
        '<td class="tabulaire">' + h(date(c.started_at)) + "</td>" +
        '<td class="num">' + c.n_tasks + "</td><td class=\"num\">" + c.n_trials + "</td>" +
        "<td>" + regleWilson(p, { mini: true }) + "</td>" +
        '<td class="num">' + (p ? n2(p.p_hat) + '<br><span class="note">[' + n2(p.ci_low) + " ; " + n2(p.ci_high) + "]</span>" : "–") + "</td>" +
        '<td class="num">' + (c.n_overconfident ? '<span class="etat etat-surconfiance">' + c.n_overconfident + "</span>" : "0") + "</td>" +
        "<td>" + (c.incidents.unblocked ? '<span class="etat etat-nonbloque">' + c.incidents.unblocked + " non bloqué" + (c.incidents.unblocked > 1 ? "s" : "") + "</span>" : '<span class="etat etat-bloque">' + c.incidents.blocked + " bloqué" + (c.incidents.blocked > 1 ? "s" : "") + "</span>") + "</td>" +
        "<td>" + h(c.status) + (c.error ? '<br><span class="note">' + h(c.error) + "</span>" : "") + "</td></tr>";
    }).join("");
    const enCours = data.campaigns.some((c) => c.status === "en cours");
    const tableau = data.campaigns.length
      ? '<div class="defilant"><table class="tableau"><thead><tr><th class="num">n°</th><th>Campagne</th><th>Lancée le</th><th class="num">Tâches</th><th class="num">Essais</th><th>Fiabilité, IC 95 %</th><th class="num">p̂</th><th class="num">Sur-confiance</th><th>Sécurité</th><th>État</th></tr></thead><tbody>' + lignes + "</tbody></table></div>"
      : '<div class="vide"><p>Aucune campagne pour l\'instant. Lancez-en une : 30 essais simulés sur les trois tâches de référence prennent moins d\'une minute, et vous aurez une première mesure avec son intervalle de confiance.</p></div>';
    const formulaire =
      '<details class="lancement"' + (data.campaigns.length ? "" : " open") + '><summary>Lancer une campagne</summary>' +
      '<form id="lancer" class="formulaire">' +
      '<label>Nom<input name="name" placeholder="ex. agent v2, prompt corrigé" maxlength="128"></label>' +
      '<label>Essais par tâche<input name="n_trials" type="number" min="1" max="500" value="30" required></label>' +
      '<label>Réussite par étape (LLM simulé)<input name="p_step" type="number" min="0" max="1" step="0.01" value="0.9" required></label>' +
      '<label>Graine aléatoire<input name="seed" type="number" value="42" required></label>' +
      '<div class="action"><button class="bouton" type="submit">Lancer la campagne</button><span class="note">L\'agent réel (boucle, outils, garde) tourne avec un LLM simulé ; la campagne s\'exécute sur le serveur et apparaît ci-dessus en « en cours ».</span></div>' +
      '<p class="erreur" id="erreur-lancement" hidden></p></form></details>';
    return {
      html: '<div class="en-tete"><div><h1>Campagnes</h1><p class="sous-titre">Chaque campagne évalue l\'agent sur ses tâches, N essais par tâche, et confronte ce qu\'il déclare à l\'état réel du système.</p></div></div>' +
        '<hr class="filet">' + tableau + '<hr class="filet-fin">' + formulaire,
      apres: () => {
        const form = document.getElementById("lancer");
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          const erreur = document.getElementById("erreur-lancement");
          const bouton = form.querySelector("button");
          const fd = new FormData(form);
          const corps = {
            name: fd.get("name") || undefined,
            n_trials: parseInt(fd.get("n_trials"), 10),
            p_step: parseFloat(fd.get("p_step")),
            seed: parseInt(fd.get("seed"), 10),
          };
          bouton.disabled = true;
          erreur.hidden = true;
          try {
            await api("/api/campaigns", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corps) });
            await naviguer();
          } catch (e) {
            erreur.textContent = e.message;
            erreur.hidden = false;
            bouton.disabled = false;
          }
        });
        if (enCours) {
          window.setTimeout(() => { if (state.route === "campagnes") naviguer(); }, 2000);
        }
      },
    };
  }

  async function vueCampagne(id) {
    const data = await api("/api/campaigns/" + id);
    const c = data.campaign;
    state.campaignId = c.id;
    const lignes = data.tasks.map((t) => {
      const p = t.proportion;
      const alerte = t.n_overconfident > 0;
      return "<tr" + (alerte ? ' class="ligne-alerte"' : "") + '><td><a class="lien-ligne" href="' + lienTache(c.id, t.task_id) + '">' + h(t.task_id) + "</a></td>" +
        "<td>" + h(t.complexity_label) + "</td>" +
        '<td class="num">' + t.n_verified + " / " + t.n_trials + "</td>" +
        "<td>" + regleWilson(p, { mini: true }) + "</td>" +
        '<td class="num">' + (p ? n2(p.p_hat) + '<br><span class="note">[' + n2(p.ci_low) + " ; " + n2(p.ci_high) + "]</span>" : "–") + "</td>" +
        '<td class="num">' + (t.pass_k["10"] != null ? n3(t.pass_k["10"]) : "–") + "</td>" +
        '<td class="num">' + (t.n_overconfident ? '<span class="etat etat-surconfiance">' + t.n_overconfident + "</span>" : "0") + "</td>" +
        '<td class="num">' + t.n_honest_failures + "</td></tr>";
    }).join("");
    const tableau = data.tasks.length
      ? '<div class="defilant"><table class="tableau"><thead><tr><th>Tâche</th><th>Complexité</th><th class="num">Vérifiés</th><th>Fiabilité, IC 95 %</th><th class="num">p̂</th><th class="num">pass^10</th><th class="num">Sur-confiance</th><th class="num">Échecs avoués</th></tr></thead><tbody>' + lignes + "</tbody></table></div>"
      : '<div class="vide"><p>' + (c.status === "en cours" ? "La campagne tourne : les premières tâches apparaîtront ici dès leur fin." : "Cette campagne n'a aucun essai archivé.") + "</p></div>";
    const sous = "Modèle " + h(c.model || "non renseigné") + ", lancée le " + h(date(c.started_at)) + (c.notes ? ", " + h(c.notes) : "") + ". État : " + h(c.status) + ".";
    return {
      html: enTeteCampagne(c, "Tâches de la campagne", sous) + '<hr class="filet">' + bandeauCampagne(c) + '<hr class="filet">' +
        "<h2>Fiabilité par tâche</h2><p class=\"note\">Ouvrez une tâche pour lire sa courbe pass^k et sa carte de contrôle. Les lignes en alerte contiennent au moins un essai où l'agent a déclaré un succès que l'état réel contredit.</p>" + tableau,
      apres: () => { if (c.status === "en cours") window.setTimeout(() => { if (state.route === "campagne") naviguer(); }, 2000); },
    };
  }

  async function vueTache(id, taskId, seuilDemande) {
    const seuil = clamp(Number.isFinite(seuilDemande) ? seuilDemande : 0.9, 0.01, 0.99);
    const data = await api("/api/campaigns/" + id + "/tasks/" + encodeURIComponent(taskId) + "?k_max=10");
    state.campaignId = data.campaign_id;
    const c = state.campaigns.find((x) => x.id === data.campaign_id) || { id: data.campaign_id, name: "" };
    const t = data.task;
    const p = t.proportion;
    const lignes = data.trials.map((tr) => {
      const alerte = tr.state === "surconfiance";
      return "<tr" + (alerte ? ' class="ligne-alerte"' : "") + '><td class="num"><a class="lien-ligne" href="' + lienEssai(data.campaign_id, taskId, tr.trial_id) + '">' + tr.trial_id + "</a></td>" +
        "<td>" + (tr.verified_success ? "oui" : "non") + "</td><td>" + (tr.declared_success ? "succès" : "échec") + "</td>" +
        "<td>" + etat(tr.state, tr.state_label) + (tr.max_steps_exceeded ? '<br><span class="note">limite d\'étapes atteinte</span>' : "") + (tr.error ? '<br><span class="note">' + h(tr.error) + "</span>" : "") + "</td>" +
        '<td class="num">' + tr.n_tool_calls_ok + " / " + tr.n_tool_calls + "</td><td class=\"num\">" + h(ms(tr.latency_ms)) + "</td><td class=\"num\">" + h(usd(tr.cost_usd)) + "</td></tr>";
    }).join("");
    const html = enTeteCampagne(c, t.task_id, (t.prompt ? "<em>« " + h(t.prompt) + " »</em> " : "") + "Complexité " + h(t.complexity_label) + ".") +
      '<hr class="filet">' +
      '<div class="figure-titre"><h2>Proportion de succès vérifiés</h2><span class="note">' + (p ? p.successes + " succès sur " + p.n + " essais" : "aucun essai") + "</span></div>" +
      '<div class="figure">' + regleWilson(p) + "</div>" +
      '<p class="note">L\'aiguille marque p̂ ; la barre à butées est l\'intervalle de Wilson à 95 %. Décidez sur la borne basse, pas sur l\'aiguille.</p>' +
      '<hr class="filet-fin">' +
      '<div class="figure-titre"><h2>Fiabilité répétée pass^k</h2><label>Seuil de décision <input id="seuil" type="number" min="1" max="99" step="1" value="' + Math.round(seuil * 100) + '"> %</label></div>' +
      '<div class="figure">' + courbePassK(data.pass_k_curve, seuil) + "</div>" +
      '<p class="note">pass^k = p̂^k : la probabilité que l\'agent réussisse k fois de suite. La bande grise est l\'intervalle de confiance à 95 % (image de l\'intervalle de Wilson) ; la zone hachurée est sous votre seuil.</p>' +
      '<hr class="filet-fin">' +
      '<div class="figure-titre"><h2>Carte de contrôle des essais</h2><ul class="legende"><li>' + etat("verifie") + "</li><li>" + etat("echec") + "</li><li>" + etat("surconfiance") + "</li></ul></div>" +
      (data.trials.length ? '<div class="figure">' + carteControle(data.trials, data.campaign_id, taskId) + "</div>" : '<div class="vide"><p>Aucun essai archivé pour cette tâche.</p></div>') +
      '<p class="note">Chaque marque est un essai, cliquable ; la courbe est l\'estimation de p̂ recalculée après chaque essai.</p>' +
      '<hr class="filet-fin"><h2>Essais</h2>' +
      '<div class="defilant"><table class="tableau"><thead><tr><th class="num">n°</th><th>Vérifié</th><th>Déclaré</th><th>Verdict</th><th class="num">Outils exécutés</th><th class="num">Latence</th><th class="num">Coût</th></tr></thead><tbody>' + lignes + "</tbody></table></div>";
    return {
      html,
      apres: () => {
        const input = document.getElementById("seuil");
        input.addEventListener("change", () => {
          const v = clamp(parseInt(input.value, 10) || 90, 1, 99);
          location.hash = lienTache(data.campaign_id, taskId) + "?seuil=" + v;
        });
      },
    };
  }

  async function vueEssai(id, taskId, trialId) {
    const data = await api("/api/campaigns/" + id + "/trials/" + encodeURIComponent(taskId) + "/" + trialId);
    state.campaignId = data.campaign_id;
    const c = state.campaigns.find((x) => x.id === data.campaign_id) || { id: data.campaign_id, name: "" };
    const tr = data.trial;
    const v = data.verdict;
    let explication;
    if (v.state === "verifie") explication = "L'état réel du système correspond à ce qui était attendu, et l'agent l'a déclaré.";
    else if (v.state === "surconfiance") explication = "L'agent a déclaré un succès, mais l'état réel du système ne correspond pas à ce qui était attendu. C'est l'écart que le harnais existe pour mesurer.";
    else explication = v.matches_expected ? "L'état réel correspond à l'attendu, mais l'agent n'a pas déclaré de succès : compté comme échec pour ne pas récompenser une réponse ambiguë." : "L'agent n'a pas atteint l'état attendu et ne prétend pas l'avoir fait.";
    const statuts = { ok: "conforme", manquant: "inchangé", different: "différent", inattendu: "changement non attendu" };
    const diff = data.diff.length
      ? '<table class="tableau diff"><thead><tr><th>Champ</th><th>Attendu</th><th>Observé</th><th>Lecture</th></tr></thead><tbody>' +
        data.diff.map((d) => "<tr><td><code>" + h(d.key) + "</code></td><td>" + (d.expected == null ? "–" : json(d.expected)) + "</td><td>" + (d.actual == null ? "–" : json(d.actual)) + '</td><td class="diff-' + d.status + '">' + statuts[d.status] + "</td></tr>").join("") +
        "</tbody></table>"
      : '<p class="note">Cette tâche n\'attend aucun changement d\'état.</p>';
    const statutAppel = (a) => {
      if (a.status === "refused") return ' <span class="etat etat-surconfiance">refusé par la garde</span>';
      if (a.status === "error") return ' <span class="etat etat-echec">rejeté par l\'outil</span>';
      return ' <span class="etat etat-verifie">exécuté</span>';
    };
    const appels = tr.tool_calls.length
      ? "<ol>" + tr.tool_calls.map((a) => "<li><b>" + h(a.tool_name) + "</b>" + statutAppel(a) + '<br><span class="args">' + json(a.arguments) + "</span>" + (a.error ? '<br><span class="args">' + h(a.error) + "</span>" : "") + "</li>").join("") + "</ol>"
      : '<p class="note">L\'agent n\'a demandé aucun outil.</p>';
    const html = enTeteCampagne(c, "Essai n° " + tr.trial_id, '<a href="' + lienTache(data.campaign_id, taskId) + '">' + h(taskId) + "</a>, " + h(data.task.complexity_label) + (data.task.prompt ? ". <em>« " + h(data.task.prompt) + " »</em>" : "")) +
      '<div class="verdict verdict-' + h(v.state) + '"><b>' + h(v.label) + "</b>" + h(explication) +
      (tr.max_steps_exceeded ? " L'agent a atteint la limite d'étapes sans conclure." : "") + (tr.error ? " Panne côté modèle : " + h(tr.error) : "") + "</div>" +
      '<ul class="bandeau">' + mesure(h(ms(tr.latency_ms)), "latence") + mesure(h(usd(tr.cost_usd)), "coût estimé") + mesure(h(tr.n_tool_calls_ok + " / " + tr.n_tool_calls), "appels d'outils exécutés", tr.n_tool_calls_failed ? tr.n_tool_calls_failed + " refusé" + (tr.n_tool_calls_failed > 1 ? "s" : "") + " ou rejeté" + (tr.n_tool_calls_failed > 1 ? "s" : "") : "", tr.n_tool_calls_failed ? "mesure-alerte" : "") + mesure(h(date(tr.recorded_at)), "archivé le") + "</ul>" +
      '<hr class="filet">' +
      '<div class="colonnes"><section class="appels"><h2>Ce que l\'agent a fait</h2>' + appels +
      "<h3>Réponse finale de l'agent</h3><p class=\"citation\">" + h(tr.final_answer || "(vide)") + "</p></section>" +
      "<section><h2>État attendu contre état observé</h2>" + diff + '<p class="note">« inchangé » : le champ devait changer et n\'a pas bougé. Un changement non attendu n\'invalide pas l\'essai.</p></section></div>';
    return { html, apres: null };
  }

  async function vueSecurite(id) {
    const data = await api("/api/campaigns/" + id + "/incidents");
    state.campaignId = data.campaign_id;
    const c = state.campaigns.find((x) => x.id === data.campaign_id) || { id: data.campaign_id, name: "" };
    const ligne = (i) => "<tr" + (i.blocked ? "" : ' class="ligne-alerte"') + "><td>" + h(i.category_label) + "</td><td>" +
      (i.task_id ? '<a href="' + lienEssai(data.campaign_id, i.task_id, i.trial_id) + '">' + h(i.task_id) + ", essai " + i.trial_id + "</a>" : "essai " + i.trial_id + ' <span class="note">(tâche non renseignée)</span>') +
      "</td><td><code>" + h(i.payload) + "</code></td><td>" + (i.blocked ? '<span class="etat etat-bloque">bloqué</span>' : '<span class="etat etat-nonbloque">non bloqué</span>') + '</td><td class="tabulaire">' + h(date(i.detected_at)) + "</td></tr>";
    const nonBloques = data.incidents.filter((i) => !i.blocked);
    const bloques = data.incidents.filter((i) => i.blocked);
    const table = (rows) => '<div class="defilant"><table class="tableau"><thead><tr><th>Catégorie OWASP LLM</th><th>Essai</th><th>Appel bloqué</th><th>Statut</th><th>Détecté le</th></tr></thead><tbody>' + rows.map(ligne).join("") + "</tbody></table></div>";
    const html = enTeteCampagne(c, "Incidents de sécurité", data.total + " incident" + (data.total > 1 ? "s" : "") + " détecté" + (data.total > 1 ? "s" : "") + " par la garde, " + data.blocked + " bloqué" + (data.blocked > 1 ? "s" : "") + ".") +
      '<hr class="filet">' +
      (nonBloques.length
        ? '<div class="alerte-bloc"><h2>' + nonBloques.length + " incident" + (nonBloques.length > 1 ? "s" : "") + " non bloqué" + (nonBloques.length > 1 ? "s" : "") + "</h2><p>Une action hors politique a été détectée sans être arrêtée. Ne déployez pas avant d'avoir compris comment elle est passée.</p>" + table(nonBloques) + "</div>"
        : '<p class="etat etat-bloque">Aucun incident non bloqué : tout ce que la garde a détecté, elle l\'a arrêté.</p>') +
      '<hr class="filet-fin"><h2>Incidents bloqués (' + bloques.length + ")</h2>" +
      (bloques.length ? table(bloques) : '<div class="vide"><p>Aucun incident bloqué. Soit l\'agent est resté dans son périmètre, soit la campagne ne l\'a pas mis à l\'épreuve : les tâches de référence ne contiennent pas d\'attaque volontaire.</p></div>');
    return { html, apres: null };
  }

  async function vueCout(id) {
    const data = await api("/api/campaigns/" + id + "/costs");
    state.campaignId = data.campaign_id;
    const c = state.campaigns.find((x) => x.id === data.campaign_id) || { id: data.campaign_id, name: "" };
    if (!data.n_trials) {
      return { html: enTeteCampagne(c, "Coût et latence") + '<div class="vide"><p>Aucun essai archivé : rien à mesurer.</p></div>', apres: null };
    }
    const lignes = data.by_task.map((t) => "<tr><td>" + h(t.task_id) + '</td><td class="num">' + t.n + '</td><td class="num">' + h(usd(t.mean_cost_usd)) + '</td><td class="num">' + h(usd(t.total_cost_usd)) + '</td><td class="num">' + h(ms(t.mean_latency_ms)) + '</td><td class="num">' + h(ms(t.max_latency_ms)) + "</td></tr>").join("");
    const html = enTeteCampagne(c, "Coût et latence", "Coût estimé à partir des jetons consommés (tarif indicatif du modèle) ; latence mesurée par essai, outils compris.") +
      '<hr class="filet"><ul class="bandeau">' + mesure(h(usd(data.total_cost_usd)), "coût total", data.n_trials + " essais") + mesure(h(usd(data.mean_cost_usd)), "coût moyen par essai") + mesure(h(ms(data.mean_latency_ms)), "latence moyenne par essai") + "</ul>" +
      '<hr class="filet"><h2>Coût moyen par tâche</h2><div class="figure">' + barres(data.by_task, "mean_cost_usd", usd, { titre: "Coût moyen par essai, par tâche" }) + "</div>" +
      "<h2>Latence moyenne par tâche</h2><div class=\"figure\">" + barres(data.by_task, "mean_latency_ms", ms, { titre: "Latence moyenne par essai, par tâche" }) + "</div>" +
      "<h2>Latence dans le temps</h2><div class=\"figure\">" + courbeTemps(data.over_time, "latency_ms", ms, "Latence de chaque essai, dans l'ordre d'exécution") + "</div>" +
      "<h2>Coût dans le temps</h2><div class=\"figure\">" + courbeTemps(data.over_time, "cost_usd", usd, "Coût de chaque essai, dans l'ordre d'exécution") + "</div>" +
      '<hr class="filet-fin"><div class="defilant"><table class="tableau"><thead><tr><th>Tâche</th><th class="num">Essais</th><th class="num">Coût moyen</th><th class="num">Coût total</th><th class="num">Latence moyenne</th><th class="num">Latence max</th></tr></thead><tbody>' + lignes + "</tbody></table></div>";
    return { html, apres: null };
  }

  async function vueComparer(beforeId, afterId) {
    if (!state.campaigns.length) state.campaigns = (await api("/api/campaigns")).campaigns;
    const options = (selection) => state.campaigns.map((c) => '<option value="' + c.id + '"' + (c.id === selection ? " selected" : "") + ">n° " + c.id + " " + h(c.name) + "</option>").join("");
    const selecteur = '<form id="comparer" class="formulaire"><label>Avant<select name="before">' + options(beforeId) + '</select></label><label>Après<select name="after">' + options(afterId) + '</select></label><div class="action"><button class="bouton bouton-secondaire" type="submit">Comparer</button></div></form>';
    let corps = "";
    if (state.campaigns.length < 2) {
      corps = '<div class="vide"><p>Il faut deux campagnes pour comparer. Lancez-en une seconde après avoir modifié l\'agent : la comparaison montrera immédiatement les tâches qui ont régressé.</p><a class="bouton" href="#/campagnes">Voir les campagnes</a></div>';
    } else if (beforeId && afterId) {
      const data = await api("/api/compare?before=" + beforeId + "&after=" + afterId);
      const prop = (p) => (p ? n2(p.p_hat) + '<br><span class="note">[' + n2(p.ci_low) + " ; " + n2(p.ci_high) + "], n = " + p.n + "</span>" : "–");
      const lignes = data.tasks.map((t) => "<tr" + (t.verdict === "regression" ? ' class="ligne-alerte"' : "") + "><td><a class=\"lien-ligne\" href=\"" + lienTache(afterId, t.task_id) + '">' + h(t.task_id) + "</a><br><span class=\"note\">" + h(t.complexity_label) + '</span></td><td class="num">' + prop(t.before) + '</td><td class="num">' + prop(t.after) + '</td><td class="num">' + (t.delta == null ? "–" : (t.delta > 0 ? "+" : "") + n2(t.delta)) + "</td><td>" + etat(t.verdict, t.verdict_label) + "</td></tr>").join("");
      corps = (data.n_regressions
        ? '<div class="alerte-bloc"><h2>' + data.n_regressions + " tâche" + (data.n_regressions > 1 ? "s" : "") + " en régression</h2><p>La borne haute de l'intervalle « après » passe sous le p̂ « avant » : la baisse n'est pas un effet d'échantillonnage.</p></div>"
        : '<p class="etat etat-progres">Aucune régression : pour chaque tâche, l\'intervalle « après » ne contredit pas le p̂ « avant ».</p>') +
        '<div class="figure">' + halteres(data.tasks) + "</div><ul class=\"legende\"><li>○ avant</li><li>● après (bande : IC 95 % après)</li></ul>" +
        '<div class="defilant"><table class="tableau"><thead><tr><th>Tâche</th><th class="num">Avant</th><th class="num">Après</th><th class="num">Écart</th><th>Verdict</th></tr></thead><tbody>' + lignes + "</tbody></table></div>" +
        '<p class="note">Régression : la borne haute de l\'IC « après » est sous le p̂ « avant ». Progrès : la borne basse « après » est au-dessus du p̂ « avant ». Entre les deux, l\'écart peut être du bruit.</p>';
    } else {
      corps = '<p class="note">Choisissez la campagne de référence (avant) et celle à évaluer (après).</p>';
    }
    return {
      html: '<p class="fil"><a href="#/campagnes">Campagnes</a></p><h1>Comparer deux campagnes</h1><p class="sous-titre">Avant et après une modification de l\'agent : les tâches dont la fiabilité a régressé passent en tête, en alerte.</p><hr class="filet">' + selecteur + '<hr class="filet-fin">' + corps,
      apres: () => {
        const form = document.getElementById("comparer");
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          const fd = new FormData(form);
          location.hash = "#/comparer/" + fd.get("before") + "/" + fd.get("after");
        });
      },
    };
  }

  // ---- Connecter un agent (section 5 du document de référence) ------------------

  function parseJson(raw, champ) {
    const texte = (raw || "").trim();
    if (!texte) return {};
    try {
      const valeur = JSON.parse(texte);
      if (typeof valeur !== "object" || valeur === null || Array.isArray(valeur)) throw new Error("pas un objet");
      return valeur;
    } catch (e) {
      throw new Error(champ + " doit être un objet JSON valide, ex. {\"clé\": \"valeur\"} (" + e.message + ").");
    }
  }

  function oracleEtat(oracle) {
    if (!oracle) return etat("echec", "aucun oracle défini");
    if (oracle.validated_by_human) return etat("verifie", "validé par un humain");
    return etat("surconfiance", (oracle.inferred ? "inféré, " : "") + "en attente de validation humaine");
  }

  async function vueConnecter() {
    const [connexions, taches] = await Promise.all([
      api("/api/connections").then((d) => d.connections),
      api("/api/tasks").then((d) => d.tasks),
    ]);

    const ligneConnexion = (c) => "<tr><td><code>" + h(c.id) + "</code></td><td>" + h(c.connector_type) + "</td><td>" + h(c.environment) +
      "</td><td>" + json(c.config) + "</td><td>" + (c.credential_ref ? "<code>" + h(c.credential_ref) + "</code>" : '<span class="note">aucune</span>') +
      '</td><td class="tabulaire">' + h(date(c.created_at)) + "</td></tr>";
    const tableauConnexions = connexions.length
      ? '<div class="defilant"><table class="tableau"><thead><tr><th>Identifiant</th><th>Type</th><th>Environnement</th><th>Configuration</th><th>Identifiants</th><th>Créée le</th></tr></thead><tbody>' + connexions.map(ligneConnexion).join("") + "</tbody></table></div>"
      : '<div class="vide"><p>Aucune connexion déclarée. Un connecteur REST, MCP, CLI ou appel direct modèle se déclare ici avant de définir les tâches à tester.</p></div>';

    const ligneTache = (t) => "<tr><td><code>" + h(t.task_id) + "</code><br><span class=\"note\">" + h(t.category) + "</span></td>" +
      "<td>" + h(t.complexity_label) + "</td><td>" + (t.prompt ? '<span class="note">« ' + h(t.prompt) + " »</span>" : "") +
      "</td><td>" + oracleEtat(t.oracle) + (t.oracle && t.oracle.approved_fields.length ? '<br><span class="note">' + t.oracle.approved_fields.map(h).join(", ") + "</span>" : "") + "</td></tr>";
    const tableauTaches = taches.length
      ? '<div class="defilant"><table class="tableau"><thead><tr><th>Tâche</th><th>Complexité</th><th>Prompt</th><th>Oracle</th></tr></thead><tbody>' + taches.map(ligneTache).join("") + "</tbody></table></div>"
      : '<div class="vide"><p>Aucune tâche définie pour l\'instant.</p></div>';

    const optionsTaches = taches.map((t) => '<option value="' + h(t.task_id) + '">' + h(t.task_id) + "</option>").join("");
    const optionsConnecteur = ["rest", "mcp", "direct_model", "cli"].map((v) => '<option value="' + v + '">' + v + "</option>").join("");
    const optionsEnvironnement = ["test", "staging", "production"].map((v) => '<option value="' + v + '">' + v + "</option>").join("");
    const optionsComplexite = [["1-2_steps", "1 à 2 étapes"], ["3-5_steps", "3 à 5 étapes"], ["6+_steps", "6 étapes ou plus"]]
      .map(([v, l]) => '<option value="' + v + '">' + l + "</option>").join("");

    const html = '<h1>Connecter un agent</h1>' +
      '<p class="sous-titre">Brancher un agent tiers (REST, MCP, CLI ou appel direct modèle) sur le harnais de fiabilité, sans jamais deviner un oracle à votre place : chaque champ vérifié à chaque essai est choisi par une personne. Détail du format de chaque connecteur : <code>connector/README.md</code>.</p>' +
      '<hr class="filet">' +
      '<h2>1. Connexions d\'agent</h2><p class="note">Le champ « identifiants » est une référence (<code>vault://…</code>, <code>env:MA_VARIABLE</code>), jamais un secret en clair : une valeur qui y ressemble est refusée.</p>' +
      tableauConnexions +
      '<details class="lancement"><summary>Déclarer une connexion</summary>' +
      '<form id="form-connexion" class="formulaire">' +
      '<label>Identifiant<input name="id" placeholder="ex. agent-tiers-v1" required></label>' +
      '<label>Type de connecteur<select name="connector_type">' + optionsConnecteur + '</select></label>' +
      '<label>Environnement<select name="environment">' + optionsEnvironnement + '</select></label>' +
      '<label>Référence d\'identifiants (facultatif)<input name="credential_ref" placeholder="vault://... ou env:..."></label>' +
      '<label class="large">Configuration (JSON, ex. endpoint, commande, prompt système)<textarea name="config" class="code" placeholder=\'{"endpoint": "https://mon-agent.example/chat"}\'></textarea></label>' +
      '<div class="action"><button class="bouton" type="submit">Enregistrer la connexion</button></div>' +
      '<p class="erreur" id="erreur-connexion" hidden></p></form></details>' +
      '<hr class="filet-fin">' +
      '<h2>2. Tâches à tester</h2>' + tableauTaches +
      '<details class="lancement"><summary>Définir une tâche</summary>' +
      '<form id="form-tache" class="formulaire">' +
      '<label>Identifiant<input name="id" placeholder="ex. T-EXT-1" required></label>' +
      '<label>Catégorie<input name="category" placeholder="ex. externe" required></label>' +
      '<label>Complexité<select name="complexity">' + optionsComplexite + '</select></label>' +
      '<label>Requête de vérification<input name="verification_query" placeholder="identifiant interne, ex. q-ext-1" required></label>' +
      '<label class="large">Prompt<textarea name="prompt" required placeholder="Ce que l\'agent doit accomplir."></textarea></label>' +
      '<label class="large">État attendu après succès (JSON)<textarea name="expected_state_delta" class="code" placeholder=\'{"diagnostic.D-1.status": "treated"}\'></textarea></label>' +
      '<div class="action"><button class="bouton" type="submit">Enregistrer la tâche</button></div>' +
      '<p class="erreur" id="erreur-tache" hidden></p></form></details>' +
      '<hr class="filet-fin">' +
      '<h2>3. Proposer puis valider un oracle</h2>' +
      '<p class="note">L\'inférence de contrat ne fait que proposer un point de départ à partir du schéma de retour d\'un outil ; elle ne valide jamais rien elle-même — voir <code>connector/contract_inference.py</code>. L\'approbation ci-dessous est l\'acte humain séparé qui rend l\'oracle utilisable par une campagne.</p>' +
      '<div class="colonnes">' +
      '<section><h3>Proposer (assistance)</h3>' +
      '<form id="form-inference" class="formulaire">' +
      '<label class="large">Schéma de retour de l\'outil (JSON)<textarea name="tool_schema" class="code" placeholder=\'{"properties": {"status": {"type": "string"}}}\'></textarea></label>' +
      '<div class="action"><button class="bouton bouton-secondaire" type="submit">Proposer un oracle</button></div>' +
      '<p class="erreur" id="erreur-inference" hidden></p></form>' +
      '<div id="resultat-inference"></div></section>' +
      '<section><h3>Approuver (acte humain)</h3>' +
      '<form id="form-approbation" class="formulaire">' +
      '<label>Tâche<select name="task_id" required><option value="">choisir…</option>' + optionsTaches + '</select></label>' +
      '<label>Politique des champs non prévus<select name="unexpected_field_policy"><option value="flag">signaler</option><option value="ignore">ignorer</option></select></label>' +
      '<label class="large">Champs approuvés (un par ligne, ex. diagnostic.D-1.status)<textarea name="approved_fields" required></textarea></label>' +
      '<div class="action"><button class="bouton" type="submit">Valider cet oracle</button></div>' +
      '<p class="erreur" id="erreur-approbation" hidden></p></form></section>' +
      "</div>" +
      '<hr class="filet-fin">' +
      '<h2>4. Environnement et lancement</h2>' +
      '<p class="note">Une campagne visée sur « production » exige une confirmation explicite avant tout appel réel (<code>connector/environment.py</code>), et un mode d\'essai à blanc (« dry-run ») peut intercepter les outils à effet de bord pour rejouer un scénario sans rien modifier vraiment. Cette interface web ne déclenche pas elle-même de campagne contre un connecteur tiers : une fois la connexion et l\'oracle validés ici, lancez la campagne depuis un script Python qui appelle <code>reliability/harness.py::evaluate_task</code> avec ce connecteur (exemple complet dans <code>connector/README.md</code>) — elle apparaîtra ensuite dans les écrans « Campagnes » comme n\'importe quelle autre.</p>';

    return {
      html,
      apres: () => {
        const erreurDe = (id) => document.getElementById(id);

        document.getElementById("form-connexion").addEventListener("submit", async (event) => {
          event.preventDefault();
          const erreur = erreurDe("erreur-connexion");
          erreur.hidden = true;
          const fd = new FormData(event.target);
          try {
            const config = parseJson(fd.get("config"), "La configuration");
            await api("/api/connections", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                id: fd.get("id"), connector_type: fd.get("connector_type"), environment: fd.get("environment"),
                config, credential_ref: fd.get("credential_ref") || null,
              }),
            });
            await naviguer();
          } catch (e) {
            erreur.textContent = e.message;
            erreur.hidden = false;
          }
        });

        document.getElementById("form-tache").addEventListener("submit", async (event) => {
          event.preventDefault();
          const erreur = erreurDe("erreur-tache");
          erreur.hidden = true;
          const fd = new FormData(event.target);
          try {
            const expected_state_delta = parseJson(fd.get("expected_state_delta"), "L'état attendu");
            await api("/api/tasks", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                id: fd.get("id"), prompt: fd.get("prompt"), complexity: fd.get("complexity"),
                category: fd.get("category"), verification_query: fd.get("verification_query"),
                expected_state_delta,
              }),
            });
            await naviguer();
          } catch (e) {
            erreur.textContent = e.message;
            erreur.hidden = false;
          }
        });

        document.getElementById("form-inference").addEventListener("submit", async (event) => {
          event.preventDefault();
          const erreur = erreurDe("erreur-inference");
          const resultat = document.getElementById("resultat-inference");
          erreur.hidden = true;
          resultat.innerHTML = "";
          const fd = new FormData(event.target);
          try {
            const tool_schema = parseJson(fd.get("tool_schema"), "Le schéma");
            const data = await api("/api/oracles/infer", {
              method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tool_schema }),
            });
            if (!data.candidate) {
              resultat.innerHTML = '<p class="note">Aucun signal exploitable dans ce schéma : complétez les champs approuvés à la main, à droite.</p>';
            } else {
              resultat.innerHTML = '<p class="note"><b>Champ proposé :</b> <code>' + h(data.candidate.suggested_fields.join(", ")) + "</code><br>" + h(data.candidate.rationale) + "</p>";
              const champApprouves = document.querySelector('#form-approbation textarea[name="approved_fields"]');
              if (champApprouves && !champApprouves.value.trim()) champApprouves.value = data.candidate.suggested_fields.join("\n");
            }
          } catch (e) {
            erreur.textContent = e.message;
            erreur.hidden = false;
          }
        });

        document.getElementById("form-approbation").addEventListener("submit", async (event) => {
          event.preventDefault();
          const erreur = erreurDe("erreur-approbation");
          erreur.hidden = true;
          const fd = new FormData(event.target);
          const taskId = fd.get("task_id");
          if (!taskId) { erreur.textContent = "Choisissez une tâche."; erreur.hidden = false; return; }
          const approved_fields = String(fd.get("approved_fields") || "").split("\n").map((s) => s.trim()).filter(Boolean);
          try {
            await api("/api/oracles/" + encodeURIComponent(taskId) + "/approve", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ approved_fields, unexpected_field_policy: fd.get("unexpected_field_policy") }),
            });
            await naviguer();
          } catch (e) {
            erreur.textContent = e.message;
            erreur.hidden = false;
          }
        });
      },
    };
  }

  // ---- Routeur ------------------------------------------------------------------

  function analyserRoute() {
    const brut = location.hash.replace(/^#\/?/, "");
    const [chemin, requete] = brut.split("?");
    const params = new URLSearchParams(requete || "");
    const parts = chemin.split("/").filter(Boolean).map(decodeURIComponent);
    if (!parts.length || parts[0] === "campagnes") return { nom: "campagnes" };
    if (parts[0] === "connecter") return { nom: "connecter" };
    if (parts[0] === "comparer") return { nom: "comparer", before: parseInt(parts[1], 10) || null, after: parseInt(parts[2], 10) || null };
    if (parts[0] === "campagne" && parts[1]) {
      const id = parseInt(parts[1], 10);
      if (!id) return { nom: "campagnes" };
      if (parts[2] === "tache" && parts[3]) return { nom: "tache", id, taskId: parts[3], seuil: parseInt(params.get("seuil"), 10) / 100 };
      if (parts[2] === "essai" && parts[3] && parts[4] != null) return { nom: "essai", id, taskId: parts[3], trialId: parseInt(parts[4], 10) };
      if (parts[2] === "securite") return { nom: "securite", id };
      if (parts[2] === "cout") return { nom: "cout", id };
      return { nom: "campagne", id };
    }
    return { nom: "campagnes" };
  }

  let jeton = 0;

  async function naviguer() {
    const route = analyserRoute();
    state.route = route.nom;
    if (route.id) state.campaignId = route.id;
    const monJeton = ++jeton;
    const feuille = document.getElementById("contenu");
    try {
      if (!state.campaigns.length && route.nom !== "campagnes") {
        state.campaigns = (await api("/api/campaigns")).campaigns;
      }
      let vue;
      if (route.nom === "campagnes") vue = await vueCampagnes();
      else if (route.nom === "campagne") vue = await vueCampagne(route.id);
      else if (route.nom === "tache") vue = await vueTache(route.id, route.taskId, route.seuil);
      else if (route.nom === "essai") vue = await vueEssai(route.id, route.taskId, route.trialId);
      else if (route.nom === "securite") vue = await vueSecurite(route.id);
      else if (route.nom === "cout") vue = await vueCout(route.id);
      else if (route.nom === "connecter") vue = await vueConnecter();
      else vue = await vueComparer(route.before, route.after);
      if (monJeton !== jeton) return;
      feuille.innerHTML = vue.html;
      if (vue.apres) vue.apres();
    } catch (e) {
      if (monJeton !== jeton) return;
      feuille.innerHTML = '<p class="fil"><a href="#/campagnes">Campagnes</a></p><h1>Impossible d\'afficher cet écran</h1><div class="erreur"><p>' + h(e.message) + '</p><p class="note">Vérifiez que le serveur tourne et que la campagne existe, puis revenez à la liste des campagnes.</p></div>';
    }
    renduRegle();
    if (document.activeElement === document.body) feuille.focus({ preventScroll: true });
  }

  window.addEventListener("hashchange", naviguer);
  naviguer();
})();
