export function shouldAutoReview(invoice) {
  if (!invoice) return false;
  if (invoice.status === "NEEDS_MANUAL_REVIEW" || invoice.status === "FAILED") return true;
  if (typeof invoice.confidence === "number" && invoice.confidence < 70) return true;
  return false;
}