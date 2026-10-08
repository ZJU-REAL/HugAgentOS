import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile('../backend/plugin_bundles/marketplace/browser-automation/web/browser/frames.js', 'utf8');
const window = {};
const decodes = [], draws = [], errors = [], images = [];
let current = {active_tab: '1', viewport_revision: 1}, width = 800, height = 600, resets = 0;
const screen = {
  dataset: {},
  get width() { return width; },
  set width(value) { resets++; width = value; },
  get height() { return height; },
  set height(value) { resets++; height = value; },
  getContext() { return {drawImage(image) { draws.push(image.id); }}; },
};
vm.runInNewContext(source, {window, Blob, createImageBitmap(blob) {
  return new Promise((resolve, reject) => decodes.push({blob, resolve, reject}));
}});
const renderer = window.installBrowserFrames(screen, () => current, () => {}, error => errors.push(error));
const frame = id => ({type: 'frame', tab_id: '1', viewport_revision: 1, frame_id: id,
  viewport: {width: 800, height: 600}});
const settle = async (index, id) => {
  const image = {id, closed: false, close() { this.closed = true; }};
  images.push(image);
  decodes[index].resolve(image);
  await new Promise(resolve => setImmediate(resolve));
};

renderer.push(frame(1), new Uint8Array([1]));
renderer.push(frame(2), new Uint8Array([2]));
renderer.push(frame(3), new Uint8Array([3]));
assert.equal(decodes.length, 1, 'Slow decoding must not fan out into concurrent decodes');
await settle(0, 1);
assert.equal(decodes.length, 2);
assert.deepEqual([...new Uint8Array(await decodes[1].blob.arrayBuffer())], [3],
  'Only the latest queued frame should be decoded');
await settle(1, 3);
assert.deepEqual(draws, [1, 3], 'Frames must keep displaying under sustained incoming traffic');
assert.equal(resets, 0, 'Stable viewport dimensions must not reset the canvas');

renderer.push(frame(4), new Uint8Array([4]));
renderer.push(frame(5), new Uint8Array([5]));
renderer.invalidate();
current = {active_tab: '2', viewport_revision: 2};
await settle(2, 4);
assert.deepEqual(draws, [1, 3], 'A decoded frame from an old tab must not be painted');
assert.equal(decodes.length, 3, 'Invalidation must discard queued frames');
current = {active_tab: '1', viewport_revision: 1};
renderer.push({...frame(6), viewport: {width: 900, height: 700}}, new Uint8Array([6]));
await settle(3, 6);
assert.equal(resets, 2);
renderer.push(frame(7), new Uint8Array([7]));
decodes[4].reject(new Error('decode failed'));
await new Promise(resolve => setImmediate(resolve));
assert.equal(errors.length, 1);
renderer.push({...frame(8), viewport: {width: 900, height: 700}}, new Uint8Array([8]));
await settle(5, 8);
assert.equal(draws.at(-1), 8, 'Decoding resumes after a failed frame');
assert.ok(images.every(image => image.closed), 'All decoded bitmaps must be released');
console.log('Bounded frame decoding, latest-frame replacement, canvas reuse and invalidation passed');
