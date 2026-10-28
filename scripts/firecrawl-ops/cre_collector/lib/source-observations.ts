/** Carry unqualified observations across a source failure without logging them. */
export class RetainedSourceError extends Error {
  readonly retainedObservations!: readonly unknown[];

  constructor(original: Error, retainedObservations: readonly unknown[]) {
    super(original.message, { cause: original });
    this.name = original.name;
    Object.defineProperty(this, "retainedObservations", {
      value: Object.freeze([...retainedObservations]),
      enumerable: false,
      writable: false,
      configurable: false,
    });
  }
}
