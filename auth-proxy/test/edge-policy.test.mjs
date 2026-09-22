import test from 'node:test';
import assert from 'node:assert/strict';
import { enforceFrontDoor, validateFrontDoorId } from '../edge-policy.mjs';

const id = '99999999-9999-4999-8999-999999999999';

test('edge ID is accepted for either managed plane but never for an unknown plane or placeholder', () => {
  for (const plane of ['api', 'admin']) {
    validateFrontDoorId(plane, id);
    validateFrontDoorId(plane, undefined);
    for (const invalid of ['', 'REPLACE_FRONT_DOOR_ID', 'not-a-guid']) assert.throws(() => validateFrontDoorId(plane, invalid));
  }
  assert.throws(() => validateFrontDoorId('unknown', id));
});

test('missing, different or duplicate Front Door headers are denied', () => {
  enforceFrontDoor({ headers: { 'x-azure-fdid': id } }, id);
  for (const value of [undefined, 'other', `${id}, ${id}`, [id, id]]) assert.throws(() => enforceFrontDoor({ headers: { 'x-azure-fdid': value } }, id));
  enforceFrontDoor({ headers: {} }, undefined);
});