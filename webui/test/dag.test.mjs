/* Regression tests for the dispatcher's pure DAG logic (A3 graph editor).
 * Run with:  node --test  (from the webui/ directory)
 * No deps — the logic lives in ../src/dag.js, framework-free for exactly this. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { parseDag, csvArr, dagIssues, layoutDag, wouldCycle } from "../src/dag.js";

test("parseDag accepts an array, rejects objects and junk", () => {
  assert.deepEqual(parseDag('[{"id":"a"}]'), [{ id: "a" }]);
  assert.equal(parseDag('{"id":"a"}'), null);   // not an array
  assert.equal(parseDag("not json"), null);
});

test("csvArr trims and drops empties; empty input -> undefined (inherit default)", () => {
  assert.deepEqual(csvArr("a, b ,,c"), ["a", "b", "c"]);
  assert.equal(csvArr("   "), undefined);
  assert.equal(csvArr(""), undefined);
});

test("dagIssues flags missing id, missing run command, and bad slice_count", () => {
  const issues = dagIssues([
    { id: "", entrypoint: "x" },
    { id: "b" },                              // no entrypoint
    { id: "c", entrypoint: "x", slice_count: 0 },
  ]);
  assert.ok(issues.some(m => /missing id/.test(m)));
  assert.ok(issues.some(m => /b: missing run command/.test(m)));
  assert.ok(issues.some(m => /slice_count must be a positive integer/.test(m)));
});

test("dagIssues catches duplicate ids and dangling depends_on", () => {
  const issues = dagIssues([
    { id: "a", entrypoint: "x" },
    { id: "a", entrypoint: "x" },
    { id: "b", entrypoint: "x", depends_on: ["ghost"] },
  ]);
  assert.ok(issues.some(m => /Duplicate id "a"/.test(m)));
  assert.ok(issues.some(m => /depends_on "ghost" not found/.test(m)));
});

test("dagIssues detects a dependency cycle", () => {
  const issues = dagIssues([
    { id: "a", entrypoint: "x", depends_on: ["b"] },
    { id: "b", entrypoint: "x", depends_on: ["a"] },
  ]);
  assert.ok(issues.some(m => /cycle/i.test(m)));
});

test("a valid linear DAG has no issues", () => {
  assert.deepEqual(dagIssues([
    { id: "a", entrypoint: "x" },
    { id: "b", entrypoint: "x", depends_on: ["a"] },
  ]), []);
});

test("wouldCycle: forward edge is fine, back edge is rejected", () => {
  const nodes = [
    { id: "a", depends_on: [] },
    { id: "b", depends_on: ["a"] },
    { id: "c", depends_on: ["b"] },
  ];
  // c -> a would be a new sink dep: a depends_on c is fine (a currently has none reaching c)
  assert.equal(wouldCycle(nodes, "a", "c"), true);   // a depends_on c closes a->...->c->b->a? a<-b<-c, adding a->c loops
  // b depends_on a already exists; adding c depends_on a is safe (no loop)
  assert.equal(wouldCycle(nodes, "c", "a"), false);
});

test("wouldCycle: self-dependency is a cycle", () => {
  const nodes = [{ id: "a", depends_on: [] }];
  assert.equal(wouldCycle(nodes, "a", "a"), true);
});

test("layoutDag places nodes in dependency-depth columns", () => {
  const nodes = [
    { id: "a", depends_on: [] },
    { id: "b", depends_on: ["a"] },
    { id: "c", depends_on: ["b"] },
  ];
  const { pos, W, H } = layoutDag(nodes);
  assert.ok(pos.a.x < pos.b.x && pos.b.x < pos.c.x, "deeper nodes sit further right");
  assert.ok(W >= 440 && H >= 190, "canvas has sane minimum dimensions");
});

test("layoutDag tolerates a cycle without throwing", () => {
  const nodes = [
    { id: "a", depends_on: ["b"] },
    { id: "b", depends_on: ["a"] },
  ];
  assert.doesNotThrow(() => layoutDag(nodes));
});
