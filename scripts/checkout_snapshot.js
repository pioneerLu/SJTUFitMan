// Allowlisted observations only; the site's own buttons perform all mutations.
() => {
  const stack = [document.querySelector('#app')?.__vue__];
  const seen = new Set();
  const scalar = x => ['string','number','boolean'].includes(typeof x) ? x : null;
  const pick = (x, keys) => Object.fromEntries(keys.map(k => [k, scalar(x?.[k])]));
  while (stack.length) {
    const view = stack.pop();
    if (!view || seen.has(view)) continue;
    seen.add(view);
    if (view.$options?.name === 'apointmentDetails') {
      const container = document.createElement('div');
      container.innerHTML = String(view.clause || '');
      return {
        component: 'apointmentDetails',
        total: scalar(view.allSun),
        dialog_visible: Boolean(view.dialogVisible),
        checked: Boolean(view.ischecked),
        terms_title: scalar(view.clauseTitle),
        terms_text: container.textContent,
        selected: Array.from(view.buyLists || [], x => pick(x, [
          'subSitename','subSiteId','scheduleTime','venuePrice','venueNum','status',
        ])),
        verification_visible: Boolean(document.querySelector('.verifybox')?.offsetParent),
      };
    }
    if (view.$options?.name === 'orderDetails') {
      return {component: 'orderDetails', order: pick(view.datas, [
        'id','orderId','orderNo','orderStateId','venueName','fieldType','totalAmount',
        'price','actualAmount','createTime','orderTime','week','isEntourage','countPrice',
      ]), spaces: Array.from(view.datas?.spaces || [], x => pick(x, [
        'venueName','scheduleDate','scheduleTime','subSitename','venuePrice',
      ]))};
    }
    stack.push(...(view.$children || []));
  }
  return null;
}
