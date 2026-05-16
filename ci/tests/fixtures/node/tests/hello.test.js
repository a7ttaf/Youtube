'use strict';

const { hello } = require('../src/hello');
const assert = require('assert');

assert.strictEqual(hello('World'), 'Hello, World!');
console.log('All tests passed');
