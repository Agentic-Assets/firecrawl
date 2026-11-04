import assert from 'node:assert/strict';
import test from 'node:test';
import { avisonYoungBaseListing } from '../../../sources/avison-young.js';

test('Avison source facts survive display flags without inventing currency', () => {
  for (const flag of [0, 1, null, 'unknown', undefined]) {
    const source = { id: 123, name: 'Example', hide_price: flag, sale_price: 123456,
      description: 'Asking 123456', external_url: 'https://example.test/property' };
    const row = avisonYoungBaseListing(source, new Map());
    assert.equal(row.rawSharpLaunch.sale_price, 123456);
    assert.equal(row.description, 'Asking 123456');
    assert.equal(row.salePriceUsd, undefined);
    assert.equal(row.salePriceText, undefined);
    assert.equal(row.avisonPublicFactsRetention.schema, 'avison-public-facts-retention/v2');
    const usd = avisonYoungBaseListing({...source, sale_currency:'USD'}, new Map());
    assert.equal(usd.salePriceUsd, 123456);
  }
});
