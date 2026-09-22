import { Denied } from './policy.mjs';

export function validateFrontDoorId(plane, id) {
  if (id === undefined) return;
  if (!['api', 'admin'].includes(plane) || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id)) throw new Error('Stage 9 requires an explicit Front Door ID');
}

export function enforceFrontDoor(request, expectedId) {
  if (expectedId === undefined) return;
  const value = request.headers['x-azure-fdid'];
  if (typeof value !== 'string' || value.toLowerCase() !== expectedId.toLowerCase()) throw new Denied();
}