// Resolve hook for tests: './protocol' -> './protocol.ts' (relative, extensionless specifiers only).
export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('.') && !/\.[cm]?[jt]sx?$/.test(specifier)) {
    try {
      return await nextResolve(`${specifier}.ts`, context);
    } catch {
      // fall through to the normal resolution
    }
  }
  return nextResolve(specifier, context);
}
