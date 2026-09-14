// A worksheet region, rendered as CELLS. `docs/SPRINT_0_6_0.md` Block F step 29.
//
// The sprint plan is explicit that this is not a picture: "Step 22 serves a spreadsheet region
// as cells, not as an image... For a sheet, JSON cells are the better answer than a picture of
// cells, and this step is a renderer decision rather than a second route." `OFFICE_SPEC.md`
// Part 7 gives the same reason at the route — "a page image of a spreadsheet answers a
// different question and is rarely the one asked".
//
// So the surface a `cells` anchor is drawn on is a TABLE, and the overlay draws on it with the
// same transform it uses on a rendered page. That is IC-9's claim and it is the reason this
// module builds a grid with a fixed cell size rather than letting the browser size columns to
// their contents: the transform is linear, so the grid has to be. A column that widened itself
// for one long string would move every box to its right.
//
// A1 IS NOT PARSED HERE EITHER, and that is `CellsAnchor`'s rule followed rather than
// restated. `jmfts_core/office/cells.py` owns the grammar; the client contract refuses a
// second copy; overlay.js refuses a third and hands the question to the surface. What this
// module does instead is LOOK THE ADDRESS UP in the region the server already resolved: the
// response carries `columns` as the letters it actually served, so a column letter is an index
// into that list and never a base-26 conversion. The only thing read out of the string is
// where the letters stop and the digits start, which is not a grammar.

import { drawHighlight, gridSurface, highlightNotice } from "./surfaces.js";

/** The cells response did not describe a region this module can draw. */
export class SheetError extends Error {
  constructor(message) {
    super(message);
    this.name = "SheetError";
  }
}

/**
 * One cell's size in the rendered grid, in CSS pixels.
 *
 * Declared in the markup rather than in the stylesheet, and that is what lets
 * `gridSurface()` know the grid's pixel extent without measuring a layout — so a box lands in
 * the same place in a browser and in a test that has no layout at all. The stylesheet still
 * owns everything that is not geometry.
 */
export const CELL = Object.freeze({ width: 104, height: 24 });

/**
 * The most rows this renderer will put on the page at once.
 *
 * `GET /documents/{id}/cells` bounds a region at `CELLS_READ_MAX` (50,000 cells,
 * `jmfts_core/office/cells.py:102`), which for a one-column region is fifty thousand rows —
 * enough table to stop a browser. This is a viewport, not a truncation of the answer: the
 * response is what it is, the caption says how much of it is on screen, and a `ref` naming a
 * row below the fold resolves to "outside the region shown here" rather than to a box drawn
 * at the edge.
 */
export const GRID_ROWS_MAX = 200;

const ADDRESS = /^\$?([A-Za-z]{1,3})\$?(\d{1,7})$/;

const el = (tag, className) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  return element;
};

const text = (value, tag, className) => {
  const element = el(tag, className);
  element.textContent = value;
  return element;
};

/**
 * Split an A1 reference into its two ends.
 *
 * Not a parser of the A1 grammar — it reads where the letters stop and the digits start, and
 * refuses anything else rather than guessing. `$` is accepted and ignored because Excel writes
 * it into a copied formula and it names the same cell either way.
 */
export function readRef(ref) {
  const ends = String(ref).split(":");
  if (ends.length > 2) {
    throw new SheetError(`"${ref}" is not a cell or a rectangle of cells`);
  }
  const read = ends.map((end) => {
    const match = ADDRESS.exec(end.trim());
    if (!match) {
      throw new SheetError(
        `"${end}" is not an A1 cell reference, so this renderer cannot say which cell of the ` +
          "region it means. jmfts_core/office/cells.py owns that grammar; this reads a " +
          "letter-and-number pair and refuses the rest rather than guessing at one."
      );
    }
    return { column: match[1].toUpperCase(), row: Number(match[2]) };
  });
  return read.length === 1 ? [read[0], read[0]] : read;
}

/**
 * Render one `DocumentCellsResponse` as a grid, with the surface a `cells` anchor draws on.
 *
 * @param {object} cells the response, verbatim
 * @returns {Element} with `surface()` on it
 */
export function renderCells(cells) {
  for (const field of ["sheet", "ref", "ref_source", "columns", "rows", "row_count"]) {
    if (!(field in cells)) {
      throw new SheetError(
        `a cells response carries ${field}, and this one does not: ` +
          `{${Object.keys(cells).join(", ")}}. See DocumentCellsResponse in ` +
          "jmfts-client/jmfts_client/contracts/document.py."
      );
    }
  }
  const columns = cells.columns;
  const [start, end] = readRef(cells.ref);
  const span = end.row - start.row + 1;
  const shown = Math.min(span, GRID_ROWS_MAX);
  if (columns.length === 0 || span <= 0) {
    throw new SheetError(
      `the served region ${cells.ref} spans ${columns.length} column(s) and ${span} row(s), ` +
        "which is no region. `ref` is the rectangle the server actually served."
    );
  }

  const values = new Map(cells.rows.map((row) => [row.row, row.values]));
  const notes = cells.cells || {};

  const table = el("table", "render-sheet-grid");
  const head = el("tr", "render-sheet-head");
  head.append(gutter(""));
  for (const letter of columns) {
    const cell = text(letter, "th", "render-sheet-column");
    cell.setAttribute("style", `width:${CELL.width}px`);
    head.append(cell);
  }
  table.append(head);

  for (let offset = 0; offset < shown; offset += 1) {
    const number = start.row + offset;
    const row = el("tr", "render-sheet-row");
    row.setAttribute("style", `height:${CELL.height}px`);
    row.append(gutter(String(number)));
    const carried = values.get(number);
    columns.forEach((letter, column) => {
      // A row the response did not carry holds nothing, and a cell whose value is null is
      // empty. Both render as an empty cell, which is what a spreadsheet shows for both —
      // this is the one place in the bundle where the two really are the same fact, because a
      // worksheet has no way to distinguish them either.
      const value = carried === undefined ? null : carried[column];
      const cell = text(value === null || value === undefined ? "" : String(value), "td", null);
      const note = notes[`${letter}${number}`];
      if (note && note.formula) {
        cell.className = "render-sheet-formula";
        cell.setAttribute(
          "title",
          note.formula_shared
            ? `${note.formula} — written on this shared-formula group's master cell, with the ` +
              "master's references"
            : note.formula
        );
      }
      row.append(cell);
    });
    table.append(row);
  }

  const surface = () =>
    gridSurface({
      kind: "sheet-region",
      // +1 on each axis for the header row and the row-number gutter, which are part of the
      // rendered grid and therefore part of the space a box is positioned in. `cellRect` adds
      // the same 1, so a caller never sees the offset and the two cannot disagree.
      columns: columns.length + 1,
      rows: shown + 1,
      cell: CELL,
      sheet: cells.sheet,
      cellRect: (ref) => rectFor(ref, columns, start.row, shown),
    });

  const figure = el("figure", "render-sheet");
  figure.append(caption(cells, span, shown));
  const scroller = el("div", "render-scroll");
  const positioned = el("div", "overlay-surface render-grid-surface");
  positioned.append(table);
  scroller.append(positioned);
  figure.append(scroller);
  figure.surface = surface;
  figure.overlayContainer = positioned;
  return figure;
}

/**
 * The grid with a passage boxed on it, for a view that has both the cells and the anchor.
 *
 * The sheet half of `surfaces.pageFigure`, and it takes what the view already fetched: a
 * `DocumentCellsResponse` from `GET /documents/{id}/cells`, and the anchor pair. Nothing here
 * calls anything.
 *
 * A `cells` anchor only. A pdf anchor on this surface is refused by `highlight()` itself —
 * a grid declares no page, and a page's rectangle drawn on whatever surface happens to be up
 * is the confidently-wrong box the whole contract is shaped against.
 */
export function gridFigure(cells, evidence) {
  const figure = renderCells(cells);
  if (!evidence) return figure;
  const found = drawHighlight(figure.overlayContainer, evidence, figure.surface());
  const notice = highlightNotice(found);
  if (notice) {
    const line = el("p", "overlay-notice");
    line.textContent = notice;
    figure.append(line);
  }
  figure.highlight = found;
  return figure;
}

function gutter(label) {
  const cell = text(label, "th", "render-sheet-gutter");
  cell.setAttribute("style", `width:${CELL.width}px`);
  return cell;
}

/**
 * Where an A1 range sits in the rendered grid, or null when it is not in it.
 *
 * Null is overlay.js's "the range is on this sheet and outside what this surface rendered",
 * which it answers `ELSEWHERE` for. That is the honest answer both for a column the region
 * does not include and for a row below `GRID_ROWS_MAX` — in both cases the passage is real and
 * this surface is not showing it.
 */
function rectFor(ref, columns, firstRow, shown) {
  const [start, end] = readRef(ref);
  const left = columns.indexOf(start.column);
  const right = columns.indexOf(end.column);
  if (left === -1 || right === -1) return null;
  const top = start.row - firstRow;
  const bottom = end.row - firstRow;
  if (top < 0 || bottom >= shown) return null;
  // +1 on both axes: the gutter column and the header row. See `surface()` above.
  return [left + 1, top + 1, right + 2, bottom + 2];
}

const REF_SOURCE = {
  request: "the region this page asked for",
  anchor: "this node's own `cells` anchor, which is the address its region was produced from",
  used_range: "the whole of what `profile:sheet` measured this sheet to hold",
};

function caption(cells, span, shown) {
  const line = el("figcaption", "render-sheet-caption");
  line.append(text(`${cells.sheet} `, "strong", null));
  line.append(text(`${cells.ref} `, "span", "mono"));
  const source = REF_SOURCE[cells.ref_source];
  line.append(
    text(
      source
        ? `— ${source}.`
        : `— served with ref_source ${cells.ref_source}, which this page has no sentence for.`,
      "span",
      "dim"
    )
  );
  if (shown < span) {
    // CUT THE DRAWING, NEVER THE DATA, AND SAY WHAT WAS WITHHELD — with the real numbers, so a
    // reader can tell a region that ends here from a region this page stopped painting. A
    // `cells` anchor below the fold answers "outside the region shown here", and this sentence
    // is what lets that be read as "below the fold" rather than "not in the region".
    const first = readRef(cells.ref)[0].row;
    line.append(
      text(
        ` Showing rows ${first} to ${first + shown - 1} — ${shown} of the region's ${span} ` +
          `rows. The response carries all ${span}; ask for a smaller ref to paint the rest.`,
        "span",
        "bad"
      )
    );
  }
  return line;
}
