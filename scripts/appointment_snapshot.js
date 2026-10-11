// Only appointment data is returned. Do not export user profiles or Vue internals.
() => {
  const root = document.querySelector('#apointmentDetails');
  if (!root) return null;
  const stack = [root.__vue__, document.querySelector('#app')?.__vue__];
  const seen = new Set();
  let view = null;
  while (stack.length) {
    const component = stack.pop();
    if (!component || seen.has(component)) continue;
    seen.add(component);
    if (component.$options?.name === 'apointmentDetails') { view = component; break; }
    stack.push(...(component.$children || []));
  }
  if (!view) return null;
  const scalar = (v) => ['string', 'number', 'boolean'].includes(typeof v) ? v : null;
  const pick = (object, keys) => Object.fromEntries(keys.map(key => [key, scalar(object?.[key])]));
  return {
    venue: pick(view.detailedData, ['venueId', 'venueName', 'venueAddr']),
    active_type: pick(view, ['activeName', 'isactiveName', 'isactiveId', 'writeOffType']),
    types: Array.from(view.tabList || [], t => pick(t, ['id', 'name', 'tension', 'writeOffType'])),
    dates: Array.from(view.weekList || [], d => pick(d, ['isdate', 'dateId', 'viewStr', 'week', 'isFull', 'isWeekend'])),
    selected_date: pick(view.timeData, ['isdate', 'dateId', 'viewStr']),
    fields: Array.from(view.topSite || [], f => pick(f, ['name', 'fieldId', 'fieldDetailStatus'])),
    slots: Array.from(view.seatArray || [], (column, index) => ({
      field_index: index,
      field_name: scalar(view.topSite?.[index]?.name),
      entries: Array.from(column || [], (cell, row) => ({
        ...pick(cell, ['price', 'venuePrice', 'status', 'remaining', 'count', 'scheduleTime']),
        time_range: typeof view.period === 'function' ? scalar(view.period(row)) : null,
        can_select: String(cell?.status) === '0',
      })),
    })),
    unpaid_order_blocks_booking: Boolean(view.arrearsTipsShow),
    loading: Boolean(view.loading),
  };
}
