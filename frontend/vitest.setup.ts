import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// jsdom does not implement scrolling; stub it for components that scroll on update.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function () {};
}

afterEach(() => {
  cleanup();
});
