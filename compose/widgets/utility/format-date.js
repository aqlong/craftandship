// compose:widget utility/format-date
// Shared date-format helper for consistent short-date rendering across widgets.
// Call: formatDate(value, style='medium') where value is a Date or ISO string.
function formatDate(value, style) {
  if (!value) return '';
  const d = (value instanceof Date) ? value : new Date(value);
  if (isNaN(d.getTime())) return String(value);
  const opts = style === 'long'
    ? { year: 'numeric', month: 'long', day: 'numeric' }
    : { year: 'numeric', month: 'short', day: 'numeric' };
  return d.toLocaleDateString('en-US', opts);
}
module.exports = { formatDate };
