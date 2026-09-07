import test from 'node:test';
import assert from 'node:assert/strict';
import { enforceFrontDoor, validateFrontDoorId } from '../edge-policy.mjs';

const id = '99999999-9999-4999-8999-999999999999';

test('edge ID must be configured explicitly for API, never admin or an unresolved placeholder', () => {
  validateFrontDoorId('api', id);
  validateFrontDoorId('api', undefined);
  for (const invalid of ['', 'REPLACE_FRONT_DOOR_ID', 'not-a-guid']) assert.throws(() => validateFrontDoorId('api', invalid));
  assert.throws(() => validateFrontDoorId('admin', id));
});

test('missing, different or duplicate Front Door headers are denied', () => {
  enforceFrontDoor({ headers: { 'x-azure-fdid': id } }, id);
  for (const value of [undefined, 'other', `${id}, ${id}`, [id, id]]) assert.throws(() => enforceFrontDoor({ headers: { 'x-azure-fdid': value } }, id));
  enforceFrontDoor({ headers: {} }, undefined);
});