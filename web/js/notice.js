// Patient and family downtime notice (S15), English and Spanish. Static text shipped in the shell: no model
// and no network call at render. It shows the hospital name and the message only, never a unit time or any
// internal clock. Prints one page per language (print.css: body.printing-notice).
// The Spanish text is Guardrail-checked but not yet reviewed by a native speaker, so it carries a visible
// label on screen and in print until it is (docs/decisions.md, S15). scripts/check_notice_guardrail.py
// checks noticeText() of each entry; re-run it whenever this text changes.
import { h, mount } from './dom.js';

export const HOSPITAL = 'Bayview Community Hospital';

const REVIEW = {
  es: 'Traducción pendiente de revisión por hablante nativo.',
  en: 'Translation pending native-speaker review.',
};
export const REVIEW_LABEL = `${REVIEW.es} / ${REVIEW.en}`;

export const NOTICE = [
  {
    lang: 'en',
    title: 'Our computer systems are down. Your care continues.',
    body: [
      'Our hospital computer systems are temporarily unavailable.',
      'Your care continues. Our staff are trained for this and are using established paper procedures.',
      'Your medications and treatments continue as planned.',
      'Some things may take a little longer than usual. Thank you for your patience.',
      'If you have questions, please ask your nurse or the charge nurse.',
    ],
    review: false,
  },
  {
    lang: 'es',
    title: 'Nuestros sistemas informáticos no están funcionando. Su atención continúa.',
    body: [
      'Los sistemas informáticos del hospital no están disponibles por el momento.',
      'Su atención continúa. Nuestro personal está preparado para esta situación y usa los procedimientos establecidos en papel.',
      'Sus medicamentos y tratamientos continúan según lo previsto.',
      'Algunas cosas pueden tardar un poco más de lo normal. Gracias por su paciencia.',
      'Si tiene preguntas, hable con su enfermero o enfermera, o con el enfermero o la enfermera a cargo del turno.',
    ],
    review: true,
  },
];

/** Everything one language's page shows, in order: what the guardrail evidence check reads. */
export const noticeText = (n) => [HOSPITAL, n.title, ...n.body, n.review ? REVIEW_LABEL : null].filter(Boolean).join('\n');

export function renderNotice(el) {
  mount(el, NOTICE.map((n) => h('section', { class: 'card notice-lang', lang: n.lang, 'aria-labelledby': `notice-title-${n.lang}` },
    h('p', { class: 'notice-hospital' }, HOSPITAL),
    h('h2', { id: `notice-title-${n.lang}`, class: 'notice-title' }, n.title),
    n.body.map((p) => h('p', { class: 'notice-text' }, p)),
    n.review ? h('p', { class: 'notice-review' },
      h('span', { lang: 'es' }, REVIEW.es), ' / ', h('span', { lang: 'en' }, REVIEW.en)) : null)));
}

/** Prints the notice only, one page per language (print.css: body.printing-notice). */
export function printNotice() {
  document.body.classList.add('printing-notice');
  window.addEventListener('afterprint', () => document.body.classList.remove('printing-notice'), { once: true });
  window.print();
}

export function initNotice() {
  renderNotice(document.getElementById('notice'));
  document.getElementById('btn-print-notice').addEventListener('click', printNotice);
}
