// Turning stored text into something a one-line preview can hold.

/**
 * Markdown as plain words, for a preview line.
 *
 * ONLY FOR PREVIEWS. A chunk's stored content is markdown, and a preview that keeps the syntax
 * reads as `# Optimal detection of changepoints ### Killick, R.` — the marks carry no meaning
 * at two lines and crowd out the words that do. Where the content is being READ rather than
 * previewed it goes through the renderer the appliance named, marks and all.
 */
export function plainWords(markdown: string | null | undefined): string {
  if (!markdown) return "";
  return markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/(\*\*|__)(.*?)\1/g, "$2")
    .replace(/(\*|_)(.*?)\1/g, "$2")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s{0,3}[-*+]\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** A value short enough to sit on one line, with the cut marked rather than silent. */
export function clip(text: string, at = 90): string {
  return text.length <= at ? text : `${text.slice(0, at - 1).trimEnd()}…`;
}
