import { readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";
import { describe, expect, it } from "vitest";

import {
  TENANT_SCOPED_ROUTES,
  TRUSTED_GATEWAY_HEADERS,
  buildTenantScopedProxy,
  proxyContextForRoute,
} from "../vite.config";

// ============================================================================
// Purpose: Guard the development proxy route and header contracts against
//   omissions without duplicating the implementation as the expected result.
// Database/ORM: None.
// Standards: Derive requested prefixes from the TypeScript compiler AST; keep
//   EXPECTED_ROUTES only as an explicit trust-boundary addition detector.
// Blast Radius: Test-only development proxy coverage.
// Connections:
//   - File: frontend/vite.config.ts -> exports proxy route/build helpers.
//   - File: frontend/src/lib/api -> contains the request literals scanned here.
//   - File: frontend/tests/devProxySecurity.test.ts -> real HTTP boundary tests.
// ============================================================================

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = path.resolve(HERE, "..");
const SRC_DIR = path.join(FRONTEND_ROOT, "src");
const SOURCE_SUFFIXES = [".cts", ".mts", ".ts", ".tsx"];

// Change-detector only; the compiler-derived assertion is the coverage proof.
const EXPECTED_ROUTES = [
  "/tenants",
  "/session",
  "/revenue",
  "/finance-close",
  "/exports",
  "/connectors",
  "/adsense",
  "/channels",
  "/org-units",
  "/groups",
  "/audit",
  "/users",
];

// Prefixes we know the application calls today. Their only job is to prove the
// scanner below still works: if a future syntax breaks it, the derived
// assertion would pass vacuously on an empty set, and this fails loudly
// instead. Shrinking this list to make a run green defeats the whole file.
const SCANNER_HEALTH_PREFIXES = [
  "/adsense",
  "/audit",
  "/channels",
  "/connectors",
  "/exports",
  "/finance-close",
  "/groups",
  "/org-units",
  "/revenue",
  "/session",
  "/tenants",
];

/** Every supported TypeScript application file under frontend/src, recursively. */
const hasApplicationSourceSuffix = (fileName: string): boolean =>
  SOURCE_SUFFIXES.some((suffix) => fileName.endsWith(suffix));

const sourceFiles = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      return sourceFiles(full);
    }
    return hasApplicationSourceSuffix(entry.name) ? [full] : [];
  });

const API_CLIENT_METHOD_NAMES = [
  "delete",
  "get",
  "getBlob",
  "patch",
  "post",
  "put",
] as const;
const API_CLIENT_METHODS = new Set<string>(API_CLIENT_METHOD_NAMES);

type StaticPrefix = {
  complete: boolean;
  text: string;
};

type EvaluationContext = {
  checker: ts.TypeChecker;
  substitutions: ReadonlyMap<ts.Symbol, ts.Expression>;
  visiting: ReadonlySet<ts.Symbol>;
};

type InspectableFunction =
  | ts.ArrowFunction
  | ts.FunctionDeclaration
  | ts.FunctionExpression
  | ts.MethodDeclaration;

/** Remove syntax wrappers that do not affect a string value. */
const unwrapExpression = (expression: ts.Expression): ts.Expression => {
  let current = expression;
  while (
    ts.isParenthesizedExpression(current) ||
    ts.isAsExpression(current) ||
    ts.isSatisfiesExpression(current) ||
    ts.isNonNullExpression(current) ||
    ts.isTypeAssertionExpression(current)
  ) {
    current = current.expression;
  }
  return current;
};

/** Resolve import aliases so declarations can be inspected across source files. */
const resolvedSymbolAt = (
  node: ts.Node,
  checker: ts.TypeChecker,
): ts.Symbol | undefined => {
  const symbol = checker.getSymbolAtLocation(node);
  return symbol && (symbol.flags & ts.SymbolFlags.Alias) !== 0
    ? checker.getAliasedSymbol(symbol)
    : symbol;
};

/** Return the immutable initializer for one identifier, if it is statically safe. */
const immutableInitializer = (
  identifier: ts.Identifier,
  context: EvaluationContext,
): { expression: ts.Expression; symbol: ts.Symbol } | undefined => {
  const symbol = resolvedSymbolAt(identifier, context.checker);
  if (!symbol) {
    return undefined;
  }
  const substituted = context.substitutions.get(symbol);
  if (substituted) {
    return { expression: substituted, symbol };
  }
  const declaration = symbol.declarations?.find(ts.isVariableDeclaration);
  if (
    !declaration?.initializer ||
    !ts.isVariableDeclarationList(declaration.parent) ||
    (declaration.parent.flags & ts.NodeFlags.Const) === 0
  ) {
    return undefined;
  }
  return { expression: declaration.initializer, symbol };
};

/** Evaluate a complete compile-time string used before the request root boundary. */
const knownString = (
  rawExpression: ts.Expression,
  context: EvaluationContext,
): string | undefined => {
  const expression = unwrapExpression(rawExpression);
  if (ts.isStringLiteral(expression) || ts.isNoSubstitutionTemplateLiteral(expression)) {
    return expression.text;
  }
  if (ts.isIdentifier(expression)) {
    const initializer = immutableInitializer(expression, context);
    if (!initializer || context.visiting.has(initializer.symbol)) {
      return undefined;
    }
    return knownString(initializer.expression, {
      ...context,
      visiting: new Set([...context.visiting, initializer.symbol]),
    });
  }
  if (
    ts.isBinaryExpression(expression) &&
    expression.operatorToken.kind === ts.SyntaxKind.PlusToken
  ) {
    const left = knownString(expression.left, context);
    const right = knownString(expression.right, context);
    return left === undefined || right === undefined ? undefined : left + right;
  }
  if (ts.isTemplateExpression(expression)) {
    let value = expression.head.text;
    for (const span of expression.templateSpans) {
      const substitution = knownString(span.expression, context);
      if (substitution === undefined) {
        return undefined;
      }
      value += substitution + span.literal.text;
    }
    return value;
  }
  if (ts.isConditionalExpression(expression)) {
    const whenTrue = knownString(expression.whenTrue, context);
    const whenFalse = knownString(expression.whenFalse, context);
    return whenTrue !== undefined && whenTrue === whenFalse ? whenTrue : undefined;
  }
  return undefined;
};

/** Enumerate statically known leading substrings across conditional expressions. */
const prefixAlternatives = (
  rawExpression: ts.Expression,
  context: EvaluationContext,
): StaticPrefix[] => {
  const expression = unwrapExpression(rawExpression);
  const complete = knownString(expression, context);
  if (complete !== undefined) {
    return [{ complete: true, text: complete }];
  }
  if (ts.isIdentifier(expression)) {
    const initializer = immutableInitializer(expression, context);
    if (!initializer || context.visiting.has(initializer.symbol)) {
      return [{ complete: false, text: "" }];
    }
    return prefixAlternatives(initializer.expression, {
      ...context,
      visiting: new Set([...context.visiting, initializer.symbol]),
    });
  }
  if (ts.isConditionalExpression(expression)) {
    return [
      ...prefixAlternatives(expression.whenTrue, context),
      ...prefixAlternatives(expression.whenFalse, context),
    ];
  }
  if (ts.isTemplateExpression(expression)) {
    let alternatives: StaticPrefix[] = [{ complete: true, text: expression.head.text }];
    for (const span of expression.templateSpans) {
      alternatives = alternatives.flatMap((prefix) =>
        prefix.complete
          ? prefixAlternatives(span.expression, context).map((substitution) => ({
              complete: substitution.complete,
              text: prefix.text + substitution.text +
                (substitution.complete ? span.literal.text : ""),
            }))
          : [prefix],
      );
    }
    return alternatives;
  }
  if (
    ts.isBinaryExpression(expression) &&
    expression.operatorToken.kind === ts.SyntaxKind.PlusToken
  ) {
    return prefixAlternatives(expression.left, context).flatMap((left) =>
      left.complete
        ? prefixAlternatives(expression.right, context).map((right) => ({
            complete: right.complete,
            text: left.text + right.text,
          }))
        : [left],
    );
  }
  return [{ complete: false, text: "" }];
};

/** Extract an exact first path segment only when the known prefix proves it. */
const rootFromPrefix = ({ complete, text }: StaticPrefix): string | undefined => {
  if (!text.startsWith("/") || text.startsWith("//")) {
    return undefined;
  }
  const boundary = text.slice(1).search(/[/?#]/u);
  const root = boundary >= 0 ? text.slice(0, boundary + 1) : complete ? text : "";
  return /^\/[a-z][a-z0-9-]*$/iu.test(root) ? root : undefined;
};

/** Collect return expressions without crossing into a nested function body. */
const returnExpressions = (body: ts.ConciseBody): ts.Expression[] => {
  if (!ts.isBlock(body)) {
    return [body];
  }
  const found: ts.Expression[] = [];
  const visit = (node: ts.Node): void => {
    if (node !== body && ts.isFunctionLike(node)) {
      return;
    }
    if (ts.isReturnStatement(node) && node.expression) {
      found.push(node.expression);
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(body);
  return found;
};

const ASSIGNMENT_OPERATORS = new Set<ts.SyntaxKind>([
  ts.SyntaxKind.AmpersandAmpersandEqualsToken,
  ts.SyntaxKind.AmpersandEqualsToken,
  ts.SyntaxKind.AsteriskAsteriskEqualsToken,
  ts.SyntaxKind.AsteriskEqualsToken,
  ts.SyntaxKind.BarBarEqualsToken,
  ts.SyntaxKind.BarEqualsToken,
  ts.SyntaxKind.CaretEqualsToken,
  ts.SyntaxKind.EqualsToken,
  ts.SyntaxKind.GreaterThanGreaterThanEqualsToken,
  ts.SyntaxKind.GreaterThanGreaterThanGreaterThanEqualsToken,
  ts.SyntaxKind.LessThanLessThanEqualsToken,
  ts.SyntaxKind.MinusEqualsToken,
  ts.SyntaxKind.PercentEqualsToken,
  ts.SyntaxKind.PlusEqualsToken,
  ts.SyntaxKind.QuestionQuestionEqualsToken,
  ts.SyntaxKind.SlashEqualsToken,
]);

/** Return whether a write target contains the exact bound parameter symbol. */
const targetContainsSymbol = (
  target: ts.Node,
  symbol: ts.Symbol,
  checker: ts.TypeChecker,
): boolean => {
  let found = false;
  const visit = (node: ts.Node): void => {
    if (ts.isIdentifier(node) && resolvedSymbolAt(node, checker) === symbol) {
      found = true;
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(target);
  return found;
};

/** Reject argument substitution when a builder can overwrite that parameter. */
const functionWritesSymbol = (
  functionNode: InspectableFunction,
  symbol: ts.Symbol,
  checker: ts.TypeChecker,
): boolean => {
  if (!functionNode.body) {
    return true;
  }
  let found = false;
  const visit = (node: ts.Node): void => {
    if (
      ts.isBinaryExpression(node) &&
      ASSIGNMENT_OPERATORS.has(node.operatorToken.kind) &&
      targetContainsSymbol(node.left, symbol, checker)
    ) {
      found = true;
      return;
    }
    if (
      (ts.isPrefixUnaryExpression(node) || ts.isPostfixUnaryExpression(node)) &&
      (node.operator === ts.SyntaxKind.PlusPlusToken ||
        node.operator === ts.SyntaxKind.MinusMinusToken) &&
      targetContainsSymbol(node.operand, symbol, checker)
    ) {
      found = true;
      return;
    }
    if (
      (ts.isForOfStatement(node) || ts.isForInStatement(node)) &&
      !ts.isVariableDeclarationList(node.initializer) &&
      targetContainsSymbol(node.initializer, symbol, checker)
    ) {
      found = true;
      return;
    }
    ts.forEachChild(node, visit);
  };
  for (const parameter of functionNode.parameters) {
    if (parameter.initializer) {
      visit(parameter.initializer);
    }
  }
  if (!found) {
    visit(functionNode.body);
  }
  return found;
};

/** Resolve one expression to immutable function implementations. */
const inspectableFunctionsForExpression = (
  rawExpression: ts.Expression,
  checker: ts.TypeChecker,
  visiting: ReadonlySet<ts.Symbol> = new Set(),
): InspectableFunction[] => {
  const expression = unwrapExpression(rawExpression);
  if (ts.isArrowFunction(expression) || ts.isFunctionExpression(expression)) {
    return [expression];
  }
  const symbol = resolvedSymbolAt(expression, checker);
  if (!symbol || visiting.has(symbol)) {
    return [];
  }
  const nestedVisiting = new Set([...visiting, symbol]);
  const functions: InspectableFunction[] = [];
  for (const declaration of symbol.declarations ?? []) {
    if (ts.isFunctionDeclaration(declaration) && declaration.body) {
      throw new Error("function-declaration request path builders are unsupported");
    } else if (ts.isMethodDeclaration(declaration) && declaration.body) {
      throw new Error("method-backed request path builders are unsupported");
    } else if (
      ts.isVariableDeclaration(declaration) &&
      declaration.initializer
    ) {
      const nested = inspectableFunctionsForExpression(
        declaration.initializer,
        checker,
        nestedVisiting,
      );
      if (nested.length > 0) {
        const list = declarationListFor(declaration);
        if (!list || (list.flags & ts.NodeFlags.Const) === 0) {
          throw new Error("called function alias is mutable");
        }
        functions.push(...nested);
      }
    } else if (ts.isPropertyAssignment(declaration)) {
      throw new Error("property-backed request path builders are unsupported");
    } else if (ts.isShorthandPropertyAssignment(declaration)) {
      throw new Error("property-backed request path builders are unsupported");
    }
  }
  return [...new Set(functions)];
};

/** Resolve a called path-builder declaration, including imported const aliases. */
const calledFunctions = (
  call: ts.CallExpression,
  checker: ts.TypeChecker,
): InspectableFunction[] =>
  inspectableFunctionsForExpression(call.expression, checker);

/** Prove the first path segment of a request expression or fail closed. */
const proveRequestRoot = (
  rawExpression: ts.Expression,
  context: EvaluationContext,
): string => {
  const expression = unwrapExpression(rawExpression);
  if (ts.isIdentifier(expression)) {
    const initializer = immutableInitializer(expression, context);
    if (!initializer || context.visiting.has(initializer.symbol)) {
      throw new Error(`identifier ${expression.text} is not an immutable static path`);
    }
    return proveRequestRoot(initializer.expression, {
      ...context,
      visiting: new Set([...context.visiting, initializer.symbol]),
    });
  }
  if (ts.isConditionalExpression(expression)) {
    const roots = [
      proveRequestRoot(expression.whenTrue, context),
      proveRequestRoot(expression.whenFalse, context),
    ];
    if (roots[0] === roots[1]) {
      return roots[0];
    }
    throw new Error(`conditional request roots disagree: ${roots.join(" vs ")}`);
  }
  if (ts.isCallExpression(expression)) {
    const symbol = resolvedSymbolAt(expression.expression, context.checker);
    if (!symbol || context.visiting.has(symbol)) {
      throw new Error("request path builder is unresolved or recursive");
    }
    const functions = calledFunctions(expression, context.checker);
    if (functions.length !== 1 || !functions[0]?.body) {
      throw new Error("request path builder must have one inspectable implementation");
    }
    const substitutions = new Map(context.substitutions);
    functions[0].parameters.forEach((parameter, index) => {
      if (!ts.isIdentifier(parameter.name)) {
        return;
      }
      const parameterSymbol = resolvedSymbolAt(parameter.name, context.checker);
      const argument = expression.arguments[index];
      if (parameterSymbol && argument) {
        if (functionWritesSymbol(functions[0], parameterSymbol, context.checker)) {
          throw new Error(
            `request path builder mutates substituted parameter ${parameter.name.text}`,
          );
        }
        substitutions.set(parameterSymbol, argument);
      }
    });
    const nestedContext: EvaluationContext = {
      checker: context.checker,
      substitutions,
      visiting: new Set([...context.visiting, symbol]),
    };
    const roots = returnExpressions(functions[0].body).map((returned) =>
      proveRequestRoot(returned, nestedContext),
    );
    if (roots.length > 0 && roots.every((root) => root === roots[0])) {
      return roots[0];
    }
    throw new Error("request path builder has no single provable root");
  }
  const roots = prefixAlternatives(expression, context).map(rootFromPrefix);
  if (roots.some((root) => !root)) {
    throw new Error("expression has no statically provable leading path segment");
  }
  if (!roots.every((root) => root === roots[0])) {
    throw new Error(`expression has conflicting request roots: ${roots.join(" vs ")}`);
  }
  return roots[0] as string;
};

/** True only for a type exposing the repository's complete client surface. */
const isApiClientType = (
  rawType: ts.Type,
  checker: ts.TypeChecker,
): boolean => {
  const receiverType = checker.getNonNullableType(rawType);
  return API_CLIENT_METHOD_NAMES.every(
    (method) => checker.getPropertyOfType(receiverType, method) !== undefined,
  );
};

const REACT_DEPENDENCY_HOOKS = new Set([
  "useCallback",
  "useEffect",
  "useImperativeHandle",
  "useLayoutEffect",
  "useMemo",
]);

/** Return the static name selected by a property or element access. */
const staticAccessName = (
  access: ts.PropertyAccessExpression | ts.ElementAccessExpression,
  checker: ts.TypeChecker,
): string | undefined => {
  if (ts.isPropertyAccessExpression(access)) {
    return access.name.text;
  }
  if (!access.argumentExpression) {
    return undefined;
  }
  const argument = unwrapExpression(access.argumentExpression);
  return ts.isNumericLiteral(argument)
    ? argument.text
    : knownString(argument, {
        checker,
        substitutions: new Map(),
        visiting: new Set(),
      });
};

/** True only for a direct call to the audited API-client hook symbol. */
const isUseApiClientCall = (
  call: ts.CallExpression,
  checker: ts.TypeChecker,
  hookSymbols: ReadonlySet<ts.Symbol>,
): boolean => {
  const callee = unwrapExpression(call.expression);
  const symbol = ts.isIdentifier(callee)
    ? resolvedSymbolAt(callee, checker)
    : undefined;
  return Boolean(
    symbol &&
    hookSymbols.has(symbol) &&
    isApiClientType(checker.getTypeAtLocation(call), checker),
  );
};

/** Allow client identity only in React dependency arrays, never data transport. */
const isAllowedDependencyReference = (
  identifier: ts.Identifier,
  checker: ts.TypeChecker,
): boolean => {
  const array = identifier.parent;
  if (!ts.isArrayLiteralExpression(array) || !array.elements.includes(identifier)) {
    return false;
  }
  const call = array.parent;
  if (!ts.isCallExpression(call) || !call.arguments.includes(array)) {
    return false;
  }
  const callee = unwrapExpression(call.expression);
  if (!ts.isIdentifier(callee) || !REACT_DEPENDENCY_HOOKS.has(callee.text)) {
    return false;
  }
  const rawSymbol = checker.getSymbolAtLocation(callee);
  return Boolean(
    rawSymbol?.declarations?.some((declaration) => {
      if (!ts.isImportSpecifier(declaration)) {
        return false;
      }
      const importDeclaration = declaration.parent.parent.parent;
      return ts.isImportDeclaration(importDeclaration) &&
        ts.isStringLiteral(importDeclaration.moduleSpecifier) &&
        importDeclaration.moduleSpecifier.text === "react";
    }),
  );
};

/** Identify the canonical hook declaration before auditing any of its references. */
const apiClientHookSymbols = (
  programSourceFiles: readonly ts.SourceFile[],
  checker: ts.TypeChecker,
): ReadonlySet<ts.Symbol> => {
  const symbols = new Set<ts.Symbol>();
  const visit = (node: ts.Node): void => {
    if (ts.isIdentifier(node)) {
      const symbol = resolvedSymbolAt(node, checker);
      if (symbol?.getName() === "useApiClient") {
        const hookType = checker.getTypeOfSymbolAtLocation(symbol, node);
        if (
          hookType.getCallSignatures().some((signature) =>
            isApiClientType(signature.getReturnType(), checker),
          )
        ) {
          symbols.add(symbol);
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  programSourceFiles.forEach(visit);
  if (symbols.size === 0) {
    throw new Error("canonical useApiClient hook symbol is missing");
  }
  return symbols;
};

/** Hook declarations/imports are metadata; every value reference must be a direct origin. */
const isHookDeclarationReference = (identifier: ts.Identifier): boolean => {
  const parent = identifier.parent;
  return (
    ((ts.isVariableDeclaration(parent) ||
      ts.isFunctionDeclaration(parent) ||
      ts.isFunctionExpression(parent)) &&
      parent.name === identifier) ||
    ts.isImportSpecifier(parent) ||
    ts.isExportSpecifier(parent)
  );
};

/** Enforce the deliberately small direct-client syntax before route discovery. */
const validateDirectApiClientContract = (
  programSourceFiles: readonly ts.SourceFile[],
  checker: ts.TypeChecker,
): ReadonlySet<ts.Symbol> => {
  const bindings = new Set<ts.Symbol>();
  const origins: ts.CallExpression[] = [];
  const hookSymbols = apiClientHookSymbols(programSourceFiles, checker);
  const visitOrigins = (node: ts.Node): void => {
    if (
      ts.isCallExpression(node) &&
      isUseApiClientCall(node, checker, hookSymbols)
    ) {
      origins.push(node);
      const declaration = node.parent;
      if (
        !ts.isVariableDeclaration(declaration) ||
        declaration.initializer !== node ||
        !ts.isIdentifier(declaration.name) ||
        declaration.type ||
        !ts.isVariableDeclarationList(declaration.parent) ||
        (declaration.parent.flags & ts.NodeFlags.Const) === 0
      ) {
        throw new Error(
          "useApiClient() must initialize one unannotated const identifier directly",
        );
      }
      const symbol = resolvedSymbolAt(declaration.name, checker);
      if (!symbol) {
        throw new Error("useApiClient() direct binding symbol is unresolved");
      }
      bindings.add(symbol);
    }
    ts.forEachChild(node, visitOrigins);
  };
  programSourceFiles.forEach(visitOrigins);
  const originSet = new Set(origins);

  for (const origin of origins) {
    const methodNames = checker
      .getPropertiesOfType(checker.getNonNullableType(checker.getTypeAtLocation(origin)))
      .map((property) => property.getName())
      .sort();
    const expected = [...API_CLIENT_METHOD_NAMES].sort();
    if (
      methodNames.length !== expected.length ||
      methodNames.some((method, index) => method !== expected[index])
    ) {
      throw new Error(
        `API-client method surface changed: ${methodNames.join(", ") || "empty"}`,
      );
    }
  }

  const visitUses = (node: ts.Node): void => {
    const normalizedFile = node.getSourceFile().fileName.replaceAll("\\", "/");
    const rawFetchReference =
      (ts.isIdentifier(node) && node.text === "fetch") ||
      ((ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) &&
        staticAccessName(node, checker) === "fetch");
    if (
      rawFetchReference &&
      !normalizedFile.endsWith("/src/lib/api/client.ts")
    ) {
      throw new Error(`raw fetch is forbidden outside API client: ${normalizedFile}`);
    }
    if (ts.isIdentifier(node)) {
      const symbol = resolvedSymbolAt(node, checker);
      if (symbol && hookSymbols.has(symbol)) {
        if (isHookDeclarationReference(node)) {
          return;
        }
        if (
          ts.isCallExpression(node.parent) &&
          node.parent.expression === node &&
          originSet.has(node.parent)
        ) {
          return;
        }
        throw new Error(
          `useApiClient hook reference ${node.text} escapes direct origin syntax`,
        );
      }
      if (symbol && bindings.has(symbol)) {
        if (ts.isVariableDeclaration(node.parent) && node.parent.name === node) {
          return;
        }
        const access = node.parent;
        if (
          (ts.isPropertyAccessExpression(access) ||
            ts.isElementAccessExpression(access)) &&
          access.expression === node &&
          API_CLIENT_METHODS.has(staticAccessName(access, checker) ?? "") &&
          ts.isCallExpression(access.parent) &&
          access.parent.expression === access
        ) {
          return;
        }
        if (isAllowedDependencyReference(node, checker)) {
          return;
        }
        throw new Error(
          `API-client binding ${node.text} escapes direct method-call syntax`,
        );
      }
    }
    ts.forEachChild(node, visitUses);
  };
  programSourceFiles.forEach(visitUses);
  return bindings;
};

/** Cheap guard before attempting provenance on arbitrary call arguments. */
const hasApiClientMethodType = (
  rawType: ts.Type,
  checker: ts.TypeChecker,
): boolean => {
  const type = checker.getNonNullableType(rawType);
  return API_CLIENT_METHOD_NAMES.some(
    (method) => checker.getPropertyOfType(type, method) !== undefined,
  );
};

/** Return whether a property chain rooted at this access is overwritten. */
const propertyChainIsWritten = (rawAccess: ts.Expression): boolean => {
  let current: ts.Node = rawAccess;
  let parent = current.parent;
  while (
    parent &&
    (((ts.isPropertyAccessExpression(parent) || ts.isElementAccessExpression(parent)) &&
      parent.expression === current) ||
      ((ts.isParenthesizedExpression(parent) ||
        ts.isAsExpression(parent) ||
        ts.isSatisfiesExpression(parent) ||
        ts.isNonNullExpression(parent) ||
        ts.isTypeAssertionExpression(parent)) &&
        parent.expression === current))
  ) {
    current = parent;
    parent = current.parent;
  }
  return Boolean(
    parent &&
      ((ts.isBinaryExpression(parent) &&
        ASSIGNMENT_OPERATORS.has(parent.operatorToken.kind) &&
        parent.left === current) ||
        ((ts.isPrefixUnaryExpression(parent) || ts.isPostfixUnaryExpression(parent)) &&
          (parent.operator === ts.SyntaxKind.PlusPlusToken ||
            parent.operator === ts.SyntaxKind.MinusMinusToken) &&
          parent.operand === current) ||
        (ts.isDeleteExpression(parent) && parent.expression === current) ||
        ((ts.isForOfStatement(parent) || ts.isForInStatement(parent)) &&
          parent.initializer === current)),
  );
};

/** Return whether a const object container stays on property-read paths only. */
const objectContainerIsStable = (
  identifier: ts.Identifier,
  symbol: ts.Symbol,
  checker: ts.TypeChecker,
): boolean => {
  let unstable = false;
  const visit = (node: ts.Node): void => {
    if (unstable) {
      return;
    }
    if (ts.isIdentifier(node) && resolvedSymbolAt(node, checker) === symbol) {
      if (ts.isVariableDeclaration(node.parent) && node.parent.name === node) {
        return;
      }
      let current: ts.Node = node;
      let parent = current.parent;
      while (
        parent &&
        (ts.isParenthesizedExpression(parent) ||
          ts.isAsExpression(parent) ||
          ts.isSatisfiesExpression(parent) ||
          ts.isNonNullExpression(parent) ||
          ts.isTypeAssertionExpression(parent)) &&
        parent.expression === current
      ) {
        current = parent;
        parent = current.parent;
      }
      if (
        parent &&
        (ts.isPropertyAccessExpression(parent) || ts.isElementAccessExpression(parent)) &&
        parent.expression === current &&
        !propertyChainIsWritten(parent)
      ) {
        return;
      }
      if (parent && ts.isSpreadAssignment(parent) && parent.expression === current) {
        return;
      }
      if (
        parent &&
        ts.isVariableDeclaration(parent) &&
        parent.initializer === current &&
        (ts.isObjectBindingPattern(parent.name) || ts.isArrayBindingPattern(parent.name))
      ) {
        return;
      }
      unstable = true;
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(identifier.getSourceFile());
  return !unstable;
};

type StaticObjectLiteral = {
  object: ts.ObjectLiteralExpression;
  unstableContainers: readonly string[];
  visiting: ReadonlySet<ts.Symbol>;
};

/** Resolve an immutable const alias to an object literal used by a spread. */
const staticObjectLiteral = (
  rawExpression: ts.Expression,
  checker: ts.TypeChecker,
  visiting: ReadonlySet<ts.Symbol>,
): StaticObjectLiteral | undefined => {
  const expression = unwrapExpression(rawExpression);
  if (ts.isObjectLiteralExpression(expression)) {
    return { object: expression, unstableContainers: [], visiting };
  }
  if (!ts.isIdentifier(expression)) {
    return undefined;
  }
  const symbol = resolvedSymbolAt(expression, checker);
  if (!symbol || visiting.has(symbol)) {
    return undefined;
  }
  const declaration = symbol.declarations?.find(ts.isVariableDeclaration);
  const list = declaration && declarationListFor(declaration);
  if (!declaration?.initializer || !list || (list.flags & ts.NodeFlags.Const) === 0) {
    return undefined;
  }
  const nested = staticObjectLiteral(
    declaration.initializer,
    checker,
    new Set([...visiting, symbol]),
  );
  if (!nested) {
    return undefined;
  }
  return {
    ...nested,
    unstableContainers: objectContainerIsStable(expression, symbol, checker)
      ? nested.unstableContainers
      : [expression.text, ...nested.unstableContainers],
  };
};

type ObjectPropertyResolution =
  | {
      expression: ts.Expression;
      kind: "found";
      unstableContainers: readonly string[];
      visiting: ReadonlySet<ts.Symbol>;
    }
  | { kind: "missing" }
  | { kind: "unknown" };

/** Resolve one property with JavaScript object-spread overwrite ordering. */
const objectLiteralPropertyInitializer = (
  object: ts.ObjectLiteralExpression,
  propertyName: string,
  checker: ts.TypeChecker,
  visiting: ReadonlySet<ts.Symbol>,
): ObjectPropertyResolution => {
  let resolution: ObjectPropertyResolution = { kind: "missing" };
  for (const property of object.properties) {
    if (ts.isSpreadAssignment(property)) {
      const spread = staticObjectLiteral(property.expression, checker, visiting);
      if (!spread) {
        resolution = { kind: "unknown" };
        continue;
      }
      const nested = objectLiteralPropertyInitializer(
        spread.object,
        propertyName,
        checker,
        spread.visiting,
      );
      if (nested.kind === "found") {
        resolution = {
          ...nested,
          unstableContainers: [
            ...spread.unstableContainers,
            ...nested.unstableContainers,
          ],
        };
      } else if (nested.kind === "unknown") {
        resolution = nested;
      }
      continue;
    }
    const nameNode = property.name;
    const name = nameNode &&
      (ts.isIdentifier(nameNode) || ts.isStringLiteral(nameNode) ||
      ts.isNumericLiteral(nameNode))
      ? nameNode.text
        : nameNode && ts.isComputedPropertyName(nameNode)
        ? knownString(nameNode.expression, {
            checker,
            substitutions: new Map(),
            visiting: new Set(),
          })
        : undefined;
    if (name === undefined) {
      resolution = { kind: "unknown" };
      continue;
    }
    if (name !== propertyName) {
      continue;
    }
    if (ts.isPropertyAssignment(property)) {
      resolution = {
        expression: property.initializer,
        kind: "found",
        unstableContainers: [],
        visiting,
      };
    } else if (ts.isShorthandPropertyAssignment(property)) {
      resolution = {
        expression: property.name,
        kind: "found",
        unstableContainers: [],
        visiting,
      };
    } else {
      resolution = { kind: "unknown" };
    }
  }
  return resolution;
};

type ResolvedPropertyInitializer = {
  expression: ts.Expression;
  unstableContainers: readonly string[];
  visiting: ReadonlySet<ts.Symbol>;
};

/** Resolve a const object/property expression back to its literal initializer. */
const immutablePropertyInitializer = (
  rawAccess: ts.Expression,
  checker: ts.TypeChecker,
  visiting: ReadonlySet<ts.Symbol>,
): ResolvedPropertyInitializer | undefined => {
  const access = unwrapExpression(rawAccess);
  if (!ts.isPropertyAccessExpression(access) && !ts.isElementAccessExpression(access)) {
    return undefined;
  }
  const propertyName = ts.isPropertyAccessExpression(access)
    ? access.name.text
    : access.argumentExpression
      ? ts.isNumericLiteral(unwrapExpression(access.argumentExpression))
        ? (unwrapExpression(access.argumentExpression) as ts.NumericLiteral).text
        : knownString(access.argumentExpression, {
            checker,
            substitutions: new Map(),
            visiting: new Set(),
          })
      : undefined;
  if (propertyName === undefined) {
    return undefined;
  }

  let container = unwrapExpression(access.expression);
  let nestedVisiting = visiting;
  let unstableContainers: string[] = [];
  while (ts.isIdentifier(container)) {
    const symbol = resolvedSymbolAt(container, checker);
    if (!symbol || nestedVisiting.has(symbol)) {
      return undefined;
    }
    const declaration = symbol.declarations?.find(ts.isVariableDeclaration);
    const list = declaration && declarationListFor(declaration);
    if (!declaration?.initializer || !list || (list.flags & ts.NodeFlags.Const) === 0) {
      return undefined;
    }
    if (!objectContainerIsStable(container, symbol, checker)) {
      unstableContainers.push(container.text);
    }
    nestedVisiting = new Set([...nestedVisiting, symbol]);
    container = unwrapExpression(declaration.initializer);
  }
  if (ts.isPropertyAccessExpression(container) || ts.isElementAccessExpression(container)) {
    const nested = immutablePropertyInitializer(container, checker, nestedVisiting);
    if (!nested) {
      return undefined;
    }
    container = unwrapExpression(nested.expression);
    nestedVisiting = nested.visiting;
    unstableContainers = [...unstableContainers, ...nested.unstableContainers];
  }
  if (ts.isArrayLiteralExpression(container)) {
    if (!/^(0|[1-9][0-9]*)$/u.test(propertyName)) {
      return undefined;
    }
    if (container.elements.some(ts.isSpreadElement)) {
      throw new Error(`API-client tuple index ${propertyName} cannot be proven`);
    }
    const index = Number(propertyName);
    const element = container.elements[index];
    return element && !ts.isOmittedExpression(element)
      ? {
          expression: element,
          unstableContainers,
          visiting: nestedVisiting,
        }
      : undefined;
  }
  if (!ts.isObjectLiteralExpression(container)) {
    return undefined;
  }
  const resolution = objectLiteralPropertyInitializer(
    container,
    propertyName,
    checker,
    nestedVisiting,
  );
  if (resolution.kind === "unknown") {
    throw new Error(`API-client object property ${propertyName} cannot be proven`);
  }
  return resolution.kind === "found"
    ? {
        expression: resolution.expression,
        unstableContainers: [
          ...unstableContainers,
          ...resolution.unstableContainers,
        ],
        visiting: resolution.visiting,
      }
    : undefined;
};

/** True only for a receiver exposing the repository's complete client surface. */
const isApiClientReceiver = (
  rawReceiver: ts.Expression,
  checker: ts.TypeChecker,
  visiting: ReadonlySet<ts.Symbol> = new Set(),
  clientParameters: ReadonlySet<ts.Symbol> = new Set(),
): boolean => {
  const receiver = unwrapExpression(rawReceiver);
  if (isApiClientType(checker.getTypeAtLocation(receiver), checker)) {
    return true;
  }
  if (ts.isPropertyAccessExpression(receiver) || ts.isElementAccessExpression(receiver)) {
    const initializer = immutablePropertyInitializer(receiver, checker, visiting);
    if (!initializer) {
      return false;
    }
    const sourcesClient = isApiClientReceiver(
      initializer.expression,
      checker,
      initializer.visiting,
      clientParameters,
    );
    if (sourcesClient && initializer.unstableContainers.length > 0) {
      throw new Error(
        `API-client object container ${initializer.unstableContainers.join(
          ", ",
        )} is mutable or escapes`,
      );
    }
    return sourcesClient;
  }
  if (!ts.isIdentifier(receiver)) {
    return false;
  }
  const symbol = resolvedSymbolAt(receiver, checker);
  if (!symbol || visiting.has(symbol)) {
    return false;
  }
  if (clientParameters.has(symbol)) {
    return true;
  }
  const binding = symbol.declarations?.find(ts.isBindingElement);
  if (binding) {
    if (
      binding.initializer &&
      isApiClientReceiver(
        binding.initializer,
        checker,
        new Set([...visiting, symbol]),
        clientParameters,
      )
    ) {
      throw new Error(
        "API-client argument escapes an unsupported call through a binding default",
      );
    }
    const initializer = immutableBindingInitializer(binding, checker, visiting);
    if (!initializer) {
      return false;
    }
    const sourcesClient = isApiClientReceiver(
      initializer.expression,
      checker,
      initializer.visiting,
      clientParameters,
    );
    if (sourcesClient && initializer.unstableContainers.length > 0) {
      throw new Error(
        `API-client object container ${initializer.unstableContainers.join(
          ", ",
        )} is mutable or escapes`,
      );
    }
    return sourcesClient;
  }
  const declaration = symbol.declarations?.find(ts.isVariableDeclaration);
  if (!declaration?.initializer) {
    return false;
  }
  const sourcesClient = isApiClientReceiver(
    declaration.initializer,
    checker,
    new Set([...visiting, symbol]),
    clientParameters,
  );
  if (!sourcesClient) {
    return false;
  }
  const list = declarationListFor(declaration);
  if (!list || (list.flags & ts.NodeFlags.Const) === 0) {
    throw new Error(`API-client receiver alias ${receiver.text} is mutable`);
  }
  return true;
};

/** Return whether a function actually invokes an API-named method on a parameter. */
const parameterUsesApiClientMethod = (
  implementation: InspectableFunction,
  parameterSymbol: ts.Symbol,
  checker: ts.TypeChecker,
): boolean => {
  let found = false;
  const visit = (node: ts.Node): void => {
    if (found) {
      return;
    }
    if (ts.isPropertyAccessExpression(node)) {
      const receiver = unwrapExpression(node.expression);
      if (
        API_CLIENT_METHODS.has(node.name.text) &&
        ts.isIdentifier(receiver) &&
        resolvedSymbolAt(receiver, checker) === parameterSymbol
      ) {
        found = true;
        return;
      }
    }
    if (ts.isElementAccessExpression(node) && node.argumentExpression) {
      const receiver = unwrapExpression(node.expression);
      const method = knownString(node.argumentExpression, {
        checker,
        substitutions: new Map(),
        visiting: new Set(),
      });
      if (
        method !== undefined &&
        API_CLIENT_METHODS.has(method) &&
        ts.isIdentifier(receiver) &&
        resolvedSymbolAt(receiver, checker) === parameterSymbol
      ) {
        found = true;
        return;
      }
    }
    ts.forEachChild(node, visit);
  };
  if (implementation.body) {
    visit(implementation.body);
  }
  return found;
};

/** Mark parameters that receive a proven API client at any inspectable call site. */
const discoverApiClientParameters = (
  sourceFiles: readonly ts.SourceFile[],
  checker: ts.TypeChecker,
): ReadonlySet<ts.Symbol> => {
  const found = new Set<ts.Symbol>();
  let changed = true;
  while (changed) {
    changed = false;
    const visit = (node: ts.Node): void => {
      if (
        ts.isCallExpression(node) &&
        !isApiClientCall(node, checker, found)
      ) {
        const implementations = calledFunctions(node, checker);
        const clientArguments = node.arguments.map((argument) => {
          const expression = unwrapExpression(argument);
          const hasStaticPropertyPath = ts.isPropertyAccessExpression(expression) ||
            (ts.isElementAccessExpression(expression) &&
              expression.argumentExpression !== undefined &&
              (ts.isNumericLiteral(
                unwrapExpression(expression.argumentExpression),
              ) ||
                knownString(expression.argumentExpression, {
                  checker,
                  substitutions: new Map(),
                  visiting: new Set(),
                }) !== undefined));
          return (
            ts.isIdentifier(expression) ||
            hasStaticPropertyPath ||
            hasApiClientMethodType(checker.getTypeAtLocation(expression), checker)
          ) && isApiClientReceiver(expression, checker, new Set(), found);
        });
        if (implementations.length === 0 && clientArguments.some(Boolean)) {
          throw new Error(
            "API-client argument escapes an unsupported call; use an inspectable immutable wrapper",
          );
        }
        for (const implementation of implementations) {
          implementation.parameters.forEach((parameter, index) => {
            if (!ts.isIdentifier(parameter.name)) {
              return;
            }
            const parameterSymbol = resolvedSymbolAt(parameter.name, checker);
            const argument = node.arguments[index];
            if (
              parameterSymbol &&
              argument &&
              parameterUsesApiClientMethod(
                implementation,
                parameterSymbol,
                checker,
              ) &&
              !found.has(parameterSymbol) &&
              clientArguments[index]
            ) {
              found.add(parameterSymbol);
              changed = true;
            }
          });
        }
      }
      ts.forEachChild(node, visit);
    };
    sourceFiles.forEach(visit);
  }
  return found;
};

/** Return whether an access selects a method from the typed API-client surface. */
const isApiClientMethodAccess = (
  rawExpression: ts.Expression,
  checker: ts.TypeChecker,
  clientParameters: ReadonlySet<ts.Symbol> = new Set(),
): boolean => {
  const expression = unwrapExpression(rawExpression);
  if (
    ts.isPropertyAccessExpression(expression) &&
    API_CLIENT_METHODS.has(expression.name.text) &&
    isApiClientReceiver(expression.expression, checker, new Set(), clientParameters)
  ) {
    return true;
  }
  if (
    ts.isElementAccessExpression(expression) &&
    isApiClientReceiver(expression.expression, checker, new Set(), clientParameters)
  ) {
    const key = expression.argumentExpression
      ? knownString(expression.argumentExpression, {
          checker,
          substitutions: new Map(),
          visiting: new Set(),
        })
      : undefined;
    if (key === undefined) {
      throw new Error("computed API-client method cannot be proven");
    }
    return API_CLIENT_METHODS.has(key);
  }
  return false;
};

/** Return the const declaration list containing a variable or binding element. */
const declarationListFor = (
  declaration: ts.VariableDeclaration | ts.BindingElement,
): ts.VariableDeclarationList | undefined => {
  let current: ts.Node | undefined = declaration;
  while (current && !ts.isVariableDeclaration(current)) {
    current = current.parent;
  }
  const variable = current && ts.isVariableDeclaration(current) ? current : undefined;
  return variable && ts.isVariableDeclarationList(variable.parent)
    ? variable.parent
    : undefined;
};

type StaticBindingPath = {
  properties: string[];
  root: ts.Expression | ts.ParameterDeclaration;
  rootIsParameter: boolean;
};

/** Resolve a nested static object/array binding path to its root initializer. */
const objectBindingPath = (
  declaration: ts.BindingElement,
  checker: ts.TypeChecker,
): StaticBindingPath | undefined => {
  const properties: string[] = [];
  let current = declaration;
  while (
    ts.isObjectBindingPattern(current.parent) ||
    ts.isArrayBindingPattern(current.parent)
  ) {
    const pattern = current.parent;
    const property = ts.isArrayBindingPattern(pattern)
      ? String(pattern.elements.indexOf(current))
      : (() => {
          const propertyNode = current.propertyName ?? current.name;
          return ts.isIdentifier(propertyNode) || ts.isStringLiteral(propertyNode)
            ? propertyNode.text
            : ts.isComputedPropertyName(propertyNode)
              ? knownString(propertyNode.expression, {
                  checker,
                  substitutions: new Map(),
                  visiting: new Set(),
                })
              : undefined;
        })();
    if (!property || current.dotDotDotToken) {
      return undefined;
    }
    properties.unshift(property);
    const owner = current.parent.parent;
    if (ts.isVariableDeclaration(owner)) {
      return owner.initializer
        ? { properties, root: owner.initializer, rootIsParameter: false }
        : undefined;
    }
    if (ts.isParameter(owner)) {
      return { properties, root: owner, rootIsParameter: true };
    }
    if (!ts.isBindingElement(owner) || owner.name !== current.parent) {
      return undefined;
    }
    current = owner;
  }
  return undefined;
};

/** Resolve a const object-binding receiver back to the value it selected. */
const immutableBindingInitializer = (
  declaration: ts.BindingElement,
  checker: ts.TypeChecker,
  visiting: ReadonlySet<ts.Symbol>,
): ResolvedPropertyInitializer | undefined => {
  const path = objectBindingPath(declaration, checker);
  if (!path) {
    return undefined;
  }
  if (path.rootIsParameter) {
    return undefined;
  }
  const list = declarationListFor(declaration);

  let expression = path.root as ts.Expression;
  let nestedVisiting = visiting;
  let unstableContainers: string[] = !list ||
    (list.flags & ts.NodeFlags.Const) === 0
    ? [ts.isIdentifier(declaration.name) ? declaration.name.text : "binding"]
    : [];
  for (const propertyName of path.properties) {
    const literal = unwrapExpression(expression);
    if (ts.isArrayLiteralExpression(literal)) {
      if (
        literal.elements.some(ts.isSpreadElement) ||
        !/^(0|[1-9][0-9]*)$/u.test(propertyName)
      ) {
        throw new Error(`API-client binding index ${propertyName} cannot be proven`);
      }
      const element = literal.elements[Number(propertyName)];
      if (!element || ts.isOmittedExpression(element)) {
        throw new Error(`API-client binding index ${propertyName} is empty`);
      }
      expression = element;
      continue;
    }
    const object = staticObjectLiteral(expression, checker, nestedVisiting);
    if (!object) {
      return undefined;
    }
    unstableContainers = [
      ...unstableContainers,
      ...object.unstableContainers,
    ];
    const resolution = objectLiteralPropertyInitializer(
      object.object,
      propertyName,
      checker,
      object.visiting,
    );
    if (resolution.kind === "unknown") {
      throw new Error(`API-client binding property ${propertyName} cannot be proven`);
    }
    if (resolution.kind === "missing") {
      return undefined;
    }
    expression = resolution.expression;
    nestedVisiting = resolution.visiting;
    unstableContainers = [
      ...unstableContainers,
      ...resolution.unstableContainers,
    ];
  }
  return { expression, unstableContainers, visiting: nestedVisiting };
};

/** Prove a nested object binding selects one method from an API-client value. */
const bindingTargetsApiClientMethod = (
  declaration: ts.BindingElement,
  checker: ts.TypeChecker,
): { rootIsParameter: boolean } | undefined => {
  const path = objectBindingPath(declaration, checker);
  if (!path) {
    return undefined;
  }
  let currentType = checker.getNonNullableType(checker.getTypeAtLocation(path.root));
  for (const [index, property] of path.properties.entries()) {
    if (
      index === path.properties.length - 1 &&
      API_CLIENT_METHODS.has(property) &&
      isApiClientType(currentType, checker)
    ) {
      return { rootIsParameter: path.rootIsParameter };
    }
    const propertySymbol = checker.getPropertyOfType(currentType, property);
    if (!propertySymbol) {
      return undefined;
    }
    currentType = checker.getNonNullableType(
      checker.getTypeOfSymbolAtLocation(propertySymbol, declaration),
    );
  }
  return undefined;
};

/** Trace destructured and assigned method aliases back to the typed client. */
const identifierTargetsApiClientMethod = (
  identifier: ts.Identifier,
  checker: ts.TypeChecker,
  visiting: ReadonlySet<ts.Symbol> = new Set(),
  clientParameters: ReadonlySet<ts.Symbol> = new Set(),
): boolean => {
  const symbol = resolvedSymbolAt(identifier, checker);
  if (!symbol || visiting.has(symbol)) {
    return false;
  }
  const nestedVisiting = new Set([...visiting, symbol]);
  for (const declaration of symbol.declarations ?? []) {
    if (ts.isBindingElement(declaration) && ts.isObjectBindingPattern(declaration.parent)) {
      const bindingMatch = bindingTargetsApiClientMethod(declaration, checker);
      if (!bindingMatch) {
        continue;
      }
      if (bindingMatch.rootIsParameter) {
        throw new Error(
          `destructured API-client parameter alias ${identifier.text} is unsupported`,
        );
      }
      const list = declarationListFor(declaration);
      if (!list || (list.flags & ts.NodeFlags.Const) === 0) {
        throw new Error(`API-client method alias ${identifier.text} is mutable`);
      }
      return true;
    }
    if (!ts.isVariableDeclaration(declaration) || !declaration.initializer) {
      continue;
    }
    const initializer = unwrapExpression(declaration.initializer);
    const targetsMethod = isApiClientMethodAccess(
      initializer,
      checker,
      clientParameters,
    ) ||
      (ts.isIdentifier(initializer) &&
        identifierTargetsApiClientMethod(
          initializer,
          checker,
          nestedVisiting,
          clientParameters,
        ));
    if (!targetsMethod) {
      continue;
    }
    const list = declarationListFor(declaration);
    if (!list || (list.flags & ts.NodeFlags.Const) === 0) {
      throw new Error(`API-client method alias ${identifier.text} is mutable`);
    }
    return true;
  }
  return false;
};

/** True only for calls through the repository's typed API-client surface. */
const isApiClientCall = (
  node: ts.CallExpression,
  checker: ts.TypeChecker,
  clientParameters: ReadonlySet<ts.Symbol>,
): boolean => {
  const callee = unwrapExpression(node.expression);
  return isApiClientMethodAccess(callee, checker, clientParameters) ||
    (ts.isIdentifier(callee) &&
      identifierTargetsApiClientMethod(
        callee,
        checker,
        new Set(),
        clientParameters,
      ));
};

/** Reject method escapes outside direct calls and const aliases we can trace. */
const assertSupportedApiClientMethodUse = (access: ts.Expression): void => {
  let current: ts.Node = access;
  let parent = current.parent;
  while (
    parent &&
    (ts.isParenthesizedExpression(parent) ||
      ts.isAsExpression(parent) ||
      ts.isSatisfiesExpression(parent) ||
      ts.isNonNullExpression(parent) ||
      ts.isTypeAssertionExpression(parent)) &&
    parent.expression === current
  ) {
    current = parent;
    parent = current.parent;
  }
  if (parent && ts.isCallExpression(parent) && parent.expression === current) {
    return;
  }
  if (
    parent &&
    ts.isVariableDeclaration(parent) &&
    parent.initializer === current &&
    ts.isVariableDeclarationList(parent.parent) &&
    (parent.parent.flags & ts.NodeFlags.Const) !== 0
  ) {
    return;
  }
  throw new Error(
    "API-client method escapes the supported direct-call or const-alias forms",
  );
};

/** Return whether an identifier labels syntax rather than evaluating a value. */
const isNonValueIdentifier = (identifier: ts.Identifier): boolean => {
  const parent = identifier.parent;
  return (
    ((ts.isVariableDeclaration(parent) ||
      ts.isBindingElement(parent) ||
      ts.isParameter(parent) ||
      ts.isFunctionDeclaration(parent) ||
      ts.isFunctionExpression(parent)) &&
      parent.name === identifier) ||
    (ts.isPropertyAccessExpression(parent) && parent.name === identifier) ||
    (ts.isPropertyAssignment(parent) && parent.name === identifier) ||
    (ts.isMethodDeclaration(parent) && parent.name === identifier)
  );
};

/** Derive all request roots in one compiler-bound source file. */
// FIX: The former literal-only scan silently dropped composed or type-erased
// API-client calls; prove actual call provenance and roots or fail closed.
const requestRootsInSourceFile = (
  sourceFile: ts.SourceFile,
  checker: ts.TypeChecker,
  directClientBindings: ReadonlySet<ts.Symbol>,
): string[] => {
  const roots = new Set<string>();
  const visit = (node: ts.Node): void => {
    let apiClientCall = false;
    if (ts.isCallExpression(node)) {
      const callee = unwrapExpression(node.expression);
      if (ts.isPropertyAccessExpression(callee) || ts.isElementAccessExpression(callee)) {
        const receiver = unwrapExpression(callee.expression);
        const receiverSymbol = ts.isIdentifier(receiver)
          ? resolvedSymbolAt(receiver, checker)
          : undefined;
        apiClientCall = Boolean(
          receiverSymbol &&
          directClientBindings.has(receiverSymbol) &&
          API_CLIENT_METHODS.has(staticAccessName(callee, checker) ?? ""),
        );
      }
    }
    if (ts.isCallExpression(node) && apiClientCall) {
      const argument = node.arguments[0];
      const position = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
      if (!argument) {
        throw new Error(
          `Cannot prove API request root at ${sourceFile.fileName}:${position.line + 1}: missing path argument`,
        );
      }
      try {
        roots.add(
          proveRequestRoot(argument, {
            checker,
            substitutions: new Map(),
            visiting: new Set(),
          }),
        );
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        throw new Error(
          `Cannot prove API request root at ${sourceFile.fileName}:${position.line + 1}: ${detail}`,
        );
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return [...roots].sort();
};

/** Build a compiler-bound single-file fixture for adversarial scanner tests. */
export const discoverRequestedPrefixesInSource = (
  source: string,
  extraClientMethods: readonly string[] = [],
): string[] => {
  const fileName = path.join(FRONTEND_ROOT, "tests", "virtual-route-scanner.ts");
  const fixturePrelude = [
    "type ReturnType<T> = T extends (...args: never[]) => infer R ? R : never;",
    "interface RouteScannerApiClient {",
    ...[...API_CLIENT_METHOD_NAMES, ...extraClientMethods].map(
      (method) => `  ${method}(path: string, ...rest: unknown[]): unknown;`,
    ),
    "}",
    "declare function useApiClient(): RouteScannerApiClient;",
    "const client = useApiClient();",
  ].join("\n");
  const compilerSource = `${fixturePrelude}\n${source}`;
  const options: ts.CompilerOptions = {
    module: ts.ModuleKind.ESNext,
    noLib: true,
    noResolve: true,
    target: ts.ScriptTarget.ES2022,
  };
  const sourceFile = ts.createSourceFile(
    fileName,
    compilerSource,
    options.target ?? ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  const host: ts.CompilerHost = {
    fileExists: (candidate) => path.resolve(candidate) === path.resolve(fileName),
    getCanonicalFileName: (candidate) => candidate.toLowerCase(),
    getCurrentDirectory: () => FRONTEND_ROOT,
    getDefaultLibFileName: () => "lib.d.ts",
    getNewLine: () => "\n",
    getSourceFile: (candidate) =>
      path.resolve(candidate) === path.resolve(fileName) ? sourceFile : undefined,
    readFile: (candidate) =>
      path.resolve(candidate) === path.resolve(fileName) ? compilerSource : undefined,
    useCaseSensitiveFileNames: () => false,
    writeFile: () => undefined,
  };
  const program = ts.createProgram({ host, options, rootNames: [fileName] });
  const checker = program.getTypeChecker();
  const directClientBindings = validateDirectApiClientContract([sourceFile], checker);
  return requestRootsInSourceFile(sourceFile, checker, directClientBindings);
};

/** Keep every compiler-loaded application file under frontend, including imports outside src. */
const applicationProgramSourceFiles = (program: ts.Program): ts.SourceFile[] =>
  program.getSourceFiles().filter((sourceFile) => {
    const relative = path.relative(FRONTEND_ROOT, sourceFile.fileName);
    return !sourceFile.isDeclarationFile &&
      relative !== "" &&
      !relative.startsWith("..") &&
      !path.isAbsolute(relative) &&
      !relative.split(path.sep).includes("node_modules") &&
      hasApplicationSourceSuffix(sourceFile.fileName);
  });

// ============================================================================
// Purpose: Derive the backend prefixes the frontend actually requests.
// Database/ORM: None.
// Standards: Inspect typed API-client call arguments, resolve immutable local and
//   imported path builders, and fail closed whenever the root cannot be proven.
// Blast Radius: Test-only coverage; no runtime or authorization effect.
// Connections:
//   - File: frontend/vite.config.ts -> TENANT_SCOPED_ROUTES.
//   - File: frontend/src/lib/api -> request call sites.
// ============================================================================
export const discoverRequestedPrefixes = (): string[] => {
  const configPath = path.join(FRONTEND_ROOT, "tsconfig.json");
  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  if (config.error) {
    throw new Error(ts.flattenDiagnosticMessageText(config.error.messageText, "\n"));
  }
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, FRONTEND_ROOT);
  if (parsed.errors.length > 0) {
    throw new Error(
      parsed.errors
        .map((error) => ts.flattenDiagnosticMessageText(error.messageText, "\n"))
        .join("\n"),
    );
  }
  const files = sourceFiles(SRC_DIR);
  const program = ts.createProgram({ options: parsed.options, rootNames: files });
  const checker = program.getTypeChecker();
  const programSourceFiles = applicationProgramSourceFiles(program);
  for (const file of files) {
    if (!program.getSourceFile(file)) {
      throw new Error(`TypeScript program omitted ${file}`);
    }
  }
  const directClientBindings = validateDirectApiClientContract(
    programSourceFiles,
    checker,
  );
  const found = new Set<string>();
  for (const sourceFile of programSourceFiles) {
    for (const root of requestRootsInSourceFile(
      sourceFile,
      checker,
      directClientBindings,
    )) {
      found.add(root);
    }
  }
  return [...found].sort();
};

/** Prefixes the application requests that the given proxy list does not cover. */
export const uncoveredPrefixes = (prefixes: string[], routes: readonly string[]): string[] =>
  prefixes.filter((prefix) => !routes.includes(prefix)).sort();

const REQUESTED_PREFIXES = discoverRequestedPrefixes();

const BACKEND_TARGET = "http://127.0.0.1:8000";

// Deliberately includes the gateway token: the point of the proxy is that this
// header is added in Node and never reaches the browser bundle.
const GATEWAY_HEADERS: [string, string][] = [
  ["X-User-ID", "00000000-0000-0000-0000-0000000000aa"],
  ["X-Role", "finance_admin"],
  ["X-UMS-Trusted-Gateway-Token", "test-token"],
];

type ProxyReqHandler = (proxyReq: {
  getHeaderNames: () => string[];
  removeHeader: (header: string) => void;
  setHeader: (header: string, value: string) => void;
}) => void;

type ConfigurableProxyEntry = {
  target: string;
  changeOrigin: boolean;
  configure: (proxy: { on: (event: string, fn: ProxyReqHandler) => void }) => void;
};

/** Narrow one built proxy entry, failing loudly rather than skipping an odd shape. */
const asConfigurableEntry = (entry: unknown, route: string): ConfigurableProxyEntry => {
  if (typeof entry !== "object" || entry === null) {
    throw new Error(`expected an object proxy entry for ${route}`);
  }
  const candidate = entry as Partial<ConfigurableProxyEntry>;
  if (typeof candidate.target !== "string" || typeof candidate.configure !== "function") {
    throw new Error(`expected ${route} to declare a target and a configure hook`);
  }
  return candidate as ConfigurableProxyEntry;
};

/** Run a route's configure hook and collect its ordered proxyReq mutations. */
const headerMutations = (
  entry: ConfigurableProxyEntry,
  route: string,
): { removed: string[]; injected: [string, string][] } => {
  const removed: string[] = [];
  const injected: [string, string][] = [];
  let handler: ProxyReqHandler | undefined;
  entry.configure({
    on: (event, fn) => {
      if (event === "proxyReq") {
        handler = fn;
      }
    },
  });
  if (!handler) {
    throw new Error(`expected ${route} to register a proxyReq handler`);
  }
  handler({
    getHeaderNames: () => [],
    removeHeader: (header: string) => {
      removed.push(header);
    },
    setHeader: (header: string, value: string) => {
      injected.push([header, value]);
    },
  });
  return { removed, injected };
};

describe("dev proxy route coverage (derived from frontend/src)", () => {
  it("audits compiler-loaded .mts/.cts and application imports outside src", () => {
    const names = [
      path.join(FRONTEND_ROOT, "src", "entry.ts"),
      path.join(FRONTEND_ROOT, "shared", "outside.mts"),
      path.join(FRONTEND_ROOT, "shared", "worker.cts"),
    ];
    const sources = new Map(
      names.map((name) => [
        path.resolve(name).toLowerCase(),
        ts.createSourceFile(name, "export {};", ts.ScriptTarget.ES2022, true),
      ]),
    );
    const options: ts.CompilerOptions = { noLib: true, noResolve: true };
    const host: ts.CompilerHost = {
      fileExists: (candidate) => sources.has(path.resolve(candidate).toLowerCase()),
      getCanonicalFileName: (candidate) => candidate.toLowerCase(),
      getCurrentDirectory: () => FRONTEND_ROOT,
      getDefaultLibFileName: () => "lib.d.ts",
      getNewLine: () => "\n",
      getSourceFile: (candidate) => sources.get(path.resolve(candidate).toLowerCase()),
      readFile: () => undefined,
      useCaseSensitiveFileNames: () => false,
      writeFile: () => undefined,
    };
    const program = ts.createProgram({ host, options, rootNames: names });
    expect(
      applicationProgramSourceFiles(program)
        .map((sourceFile) => path.extname(sourceFile.fileName))
        .sort(),
    ).toEqual([".cts", ".mts", ".ts"]);
  });

  it("still finds the API calls we know exist, so the scan cannot pass vacuously", () => {
    for (const prefix of SCANNER_HEALTH_PREFIXES) {
      expect(REQUESTED_PREFIXES, `scanner lost ${prefix}`).toContain(prefix);
    }
  });

  it('resolves the composed request root in client.get("/" + "reports")', () => {
    const prefixes = discoverRequestedPrefixesInSource('client.get("/" + "reports");');
    expect(prefixes).toEqual(["/reports"]);
    expect(uncoveredPrefixes(prefixes, TENANT_SCOPED_ROUTES)).toEqual(["/reports"]);
  });

  it("tracks the typed API-client surface after the local binding is renamed", () => {
    const source = [
      "const api = useApiClient();",
      'api.get("/reports/raw-files");',
    ].join("\n");
    const prefixes = discoverRequestedPrefixesInSource(source);
    expect(prefixes).toEqual(["/reports"]);
    expect(uncoveredPrefixes(prefixes, TENANT_SCOPED_ROUTES)).toEqual(["/reports"]);
  });

  it("fails closed when the canonical API-client hook itself is aliased", () => {
    const source = [
      "const factory = useApiClient;",
      "const api = factory();",
      'api.get("/reports/raw-files");',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient hook reference useApiClient escapes direct origin syntax/iu,
    );
  });

  it("fails closed when a client origin is hidden by a type annotation", () => {
    const source = [
      "const api: { get(path: string): unknown } = useApiClient();",
      'api.get("/reports/raw-files");',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when the client crosses a receiver-parameter boundary", () => {
    const source = [
      "function load(api: { get(path: string): unknown }) {",
      '  api.get("/reports/raw-files");',
      "}",
      "load(useApiClient());",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when the client crosses a function-alias boundary", () => {
    const source = [
      "function load(api: { get(path: string): unknown }) {",
      '  api.get("/reports/raw-files");',
      "}",
      "const runner = load;",
      "runner(useApiClient());",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when the client crosses an object-method boundary", () => {
    const source = [
      "const loaders = {",
      "  load(api: { get(path: string): unknown }) {",
      '    api.get("/reports/raw-files");',
      "  },",
      "};",
      "loaders.load(useApiClient());",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when the client crosses a function-valued property", () => {
    const source = [
      "const loaders = {",
      "  load: (api: { get(path: string): unknown }) => {",
      '    api.get("/reports/raw-files");',
      "  },",
      "};",
      "loaders.load(useApiClient());",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when the client crosses a shorthand-property function", () => {
    const source = [
      "function load(api: { get(path: string): unknown }) {",
      '  api.get("/reports/raw-files");',
      "}",
      "const loaders = { load };",
      "loaders.load(useApiClient());",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when a client argument escapes an unsupported call", () => {
    const source = [
      "declare function opaque(value: { get(path: string): unknown }): void;",
      "opaque(useApiClient());",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when an erased client alias escapes an unsupported call", () => {
    const source = [
      "declare function forward(value: unknown): void;",
      "const erased: unknown = useApiClient();",
      "forward(erased);",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when an erased client tuple entry escapes an unsupported call", () => {
    const source = [
      "declare function forward(value: unknown): void;",
      "const clients: readonly [unknown] = [useApiClient()];",
      "forward(clients[0]);",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when a destructured erased tuple client escapes", () => {
    const source = [
      "declare function forward(value: unknown): void;",
      "const [erased]: readonly [unknown] = [useApiClient()];",
      "forward(erased);",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when a destructuring default supplies an erased client", () => {
    const source = [
      "declare function forward(value: unknown): void;",
      "const { api = useApiClient() }: { api?: unknown } = {};",
      "forward(api);",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when the client is stored in an object literal", () => {
    const source = [
      "const holder: { api: { get(path: string): unknown } } = {",
      "  api: useApiClient(),",
      "};",
      'holder.api.get("/reports/raw-files");',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when the client is transported through an object spread", () => {
    const source = [
      "const source = { api: useApiClient() };",
      "const holder: { api: { get(path: string): unknown } } = { ...source };",
      'holder.api.get("/reports/raw-files");',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when the client is selected by object destructuring", () => {
    const source = [
      "const source = { api: useApiClient() };",
      "const { api }: { api: { get(path: string): unknown } } = source;",
      'api.get("/reports/raw-files");',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed when a narrowed client container property is overwritten", () => {
    const source = [
      "const holder: { api: { get(path: string): unknown } } = {",
      "  api: useApiClient(),",
      "};",
      "holder.api = { get: (_path: string) => undefined };",
      'holder.api.get("/reports/raw-files");',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed on a nullable annotated client binding", () => {
    const source = [
      "const api: ReturnType<typeof useApiClient> | undefined = useApiClient();",
      'api?.get("/reports/raw-files");',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("tracks a literal computed method on the typed API-client surface", () => {
    const source = [
      "const api = useApiClient();",
      'api["get"]("/reports/raw-files");',
    ].join("\n");
    expect(discoverRequestedPrefixesInSource(source)).toEqual(["/reports"]);
  });

  it("fails closed on a destructured API-client method alias", () => {
    const source = [
      "const api = useApiClient();",
      "const { post } = api;",
      'post("/reports/jobs");',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /API-client binding api escapes direct method-call syntax/iu,
    );
  });

  it("fails closed on the narrowed-method destructuring counterexample", () => {
    const source = [
      "const api: { get(path: string): unknown } = useApiClient();",
      "const { get } = api;",
      'get("/reports/raw-files");',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed on a client hidden by nested object destructuring", () => {
    const source = [
      "const holder = { api: useApiClient() };",
      "const { api: { get } } = holder;",
      'get("/reports/raw-files");',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it("fails closed on a destructured API-client parameter alias", () => {
    const source = [
      "function load({ get }: RouteScannerApiClient) {",
      '  get("/reports/raw-files");',
      "}",
      "load(useApiClient());",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it.each([
    [
      "bound wrapper",
      [
        "const api = useApiClient();",
        "const get = api.get.bind(api);",
        'get("/reports/raw-files");',
      ].join("\n"),
    ],
    [
      "callback forwarding",
      [
        "declare function forward(value: unknown): void;",
        "const api = useApiClient();",
        "forward(api.get);",
      ].join("\n"),
    ],
    [
      "const-alias forwarding",
      [
        "declare function forward(value: unknown): void;",
        "const api = useApiClient();",
        "const get = api.get;",
        "forward(get);",
      ].join("\n"),
    ],
    [
      "destructured-alias forwarding",
      [
        "declare function forward(value: unknown): void;",
        "const api = useApiClient();",
        "const { post } = api;",
        "forward(post);",
      ].join("\n"),
    ],
  ])("fails closed on unsupported API-client method escape through %s", (_label, source) => {
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /API-client binding api escapes direct method-call syntax/iu,
    );
  });

  it.each([
    [
      "identity-return wrapper",
      [
        "function identity<T>(value: T): T { return value; }",
        "const erased = identity<unknown>(useApiClient());",
      ].join("\n"),
    ],
    [
      "function alias returning the client",
      [
        "function erase(value: RouteScannerApiClient): unknown { return value; }",
        "const alias = erase;",
        "const erased = alias(useApiClient());",
      ].join("\n"),
    ],
    [
      "object-return wrapper",
      "const holder = { api: useApiClient() };",
    ],
    [
      "tuple transport",
      "const tuple = [useApiClient()] as const;",
    ],
    [
      "constructor transport",
      [
        "class Box { constructor(readonly value: unknown) {} }",
        "const box = new Box(useApiClient());",
      ].join("\n"),
    ],
    [
      "zero-argument closure",
      "const make = (): unknown => useApiClient();",
    ],
    [
      "class getter",
      "class Holder { get api(): unknown { return useApiClient(); } }",
    ],
    [
      "logical expression",
      [
        "declare const enabled: boolean;",
        "const erased = enabled && useApiClient();",
      ].join("\n"),
    ],
    [
      "later assignment",
      [
        "let erased: unknown;",
        "erased = useApiClient();",
      ].join("\n"),
    ],
    [
      "destructuring assignment",
      [
        "let erased: unknown;",
        "({ api: erased } = { api: useApiClient() });",
      ].join("\n"),
    ],
    [
      "object rest",
      "const { keep, ...rest } = { keep: true, api: useApiClient() };",
    ],
  ])("fails closed when useApiClient is laundered through %s", (_label, source) => {
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /useApiClient\(\) must initialize one unannotated const identifier directly/iu,
    );
  });

  it.each([
    [
      "wrapper argument",
      [
        "const api = useApiClient();",
        "declare function erase(value: unknown): unknown;",
        "const erased = erase(api);",
      ].join("\n"),
    ],
    [
      "mutable object holder",
      [
        "const api = useApiClient();",
        "const holder: { value: unknown } = { value: undefined };",
        "holder.value = api;",
      ].join("\n"),
    ],
    [
      "mutable tuple holder",
      [
        "const api = useApiClient();",
        "const tuple: unknown[] = [];",
        "tuple[0] = api;",
      ].join("\n"),
    ],
    [
      "higher-order callback",
      [
        "const api = useApiClient();",
        "declare function later(callback: () => unknown): void;",
        "later(() => api);",
      ].join("\n"),
    ],
  ])("fails closed when a direct client binding escapes through %s", (_label, source) => {
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /API-client binding api escapes direct method-call syntax/iu,
    );
  });

  it.each([
    ["direct", 'fetch("/reports/raw-files");'],
    [
      "aliased computed member",
      [
        'const send = globalThis["fetch"];',
        'send("/reports/raw-files");',
      ].join("\n"),
    ],
  ])("fails closed on %s raw fetch outside the audited API client", (_label, source) => {
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /raw fetch is forbidden outside API client/iu,
    );
  });

  it("fails closed when the API-client method surface gains an unscanned transport", () => {
    expect(() => discoverRequestedPrefixesInSource("", ["trace"])).toThrow(
      /API-client method surface changed:.*trace/iu,
    );
  });

  it("does not mistake a local hook lookalike for an approved React dependency sink", () => {
    const source = [
      "declare function useEffect(effect: () => void, dependencies: unknown[]): void;",
      "const api = useApiClient();",
      "useEffect(() => undefined, [api]);",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /API-client binding api escapes direct method-call syntax/iu,
    );
  });

  it("resolves a const-backed interpolation in client.get(`/${resource}/daily`)", () => {
    // Build the interpolation delimiter without putting a template-like string
    // in this test module; the virtual source is exactly the regression shape.
    const interpolation = String.fromCharCode(36, 123);
    const source = [
      'const resource = "reports";',
      ["client.get(`/", interpolation, "resource}/daily`);"].join(""),
    ].join("\n");
    const prefixes = discoverRequestedPrefixesInSource(source);
    expect(prefixes).toEqual(["/reports"]);
    expect(uncoveredPrefixes(prefixes, TENANT_SCOPED_ROUTES)).toEqual(["/reports"]);
  });

  it("fails closed when a dynamic expression controls the request root", () => {
    const interpolation = String.fromCharCode(36, 123);
    const source = [
      "declare function runtimeResource(): string;",
      "const resource = runtimeResource();",
      ["client.get(`/", interpolation, "resource}/daily`);"].join(""),
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /Cannot prove API request root.*statically provable/iu,
    );
  });

  it("fails closed when a path builder reassigns a substituted parameter", () => {
    const interpolation = String.fromCharCode(36, 123);
    const source = [
      "declare function runtimeResource(): string;",
      "const build = (root: string) => {",
      "  root = runtimeResource();",
      ["  return `/", interpolation, "root}/daily`;"].join(""),
      "};",
      'client.get(build("reports"));',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /request path builder mutates substituted parameter root/iu,
    );
  });

  it("fails closed when a default initializer reassigns a builder parameter", () => {
    const interpolation = String.fromCharCode(36, 123);
    const source = [
      "declare function runtimeResource(): string;",
      "const build = (",
      "  root: string,",
      "  ignored = (root = runtimeResource()),",
      ") => {",
      ["  return `/", interpolation, "root}/daily`;"].join(""),
      "};",
      'client.get(build("reports"));',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /request path builder mutates substituted parameter root/iu,
    );
  });

  it("fails closed when a mutable binding replaces a path builder", () => {
    const source = [
      "declare const runtimeBuilder: () => string;",
      'let build = () => "/reports";',
      "build = runtimeBuilder;",
      "client.get(build());",
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /called function alias is mutable|request path builder must have one inspectable implementation/iu,
    );
  });

  it("fails closed when a function-declaration path builder is reassigned", () => {
    const source = [
      "declare const runtimeBuilder: (path: string) => string;",
      "function route(path: string) { return path; }",
      "route = runtimeBuilder;",
      'client.get(route("/reports"));',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /function-declaration request path builders are unsupported/iu,
    );
  });

  it.each([
    [
      "new receiver",
      [
        "class Routes { route(path: string) { return path; } }",
        'client.get(new Routes().route("/reports"));',
      ].join("\n"),
    ],
    [
      "const instance receiver",
      [
        "class Routes { route(path: string) { return path; } }",
        "const routes = new Routes();",
        'client.get(routes.route("/reports"));',
      ].join("\n"),
    ],
    [
      "object-property builder",
      [
        "const builders = { route: (path: string) => path };",
        'client.get(builders.route("/reports"));',
      ].join("\n"),
    ],
  ])("fails closed on a method/property path builder through %s", (_label, source) => {
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /method-backed request path builders are unsupported|property-backed request path builders are unsupported/iu,
    );
  });

  it("fails closed when for-of overwrites a substituted builder parameter", () => {
    const interpolation = String.fromCharCode(36, 123);
    const source = [
      "declare function runtimeRoots(): string[];",
      "const build = (root: string) => {",
      "  for (root of runtimeRoots()) {}",
      ["  return `/", interpolation, "root}/daily`;"].join(""),
      "};",
      'client.get(build("reports"));',
    ].join("\n");
    expect(() => discoverRequestedPrefixesInSource(source)).toThrow(
      /request path builder mutates substituted parameter root/iu,
    );
  });

  it("ignores API-looking paths that are not API-client call arguments", () => {
    const source = '// client.get("/reports/raw-files")\nconst x = "/hidden";\n';
    expect(discoverRequestedPrefixesInSource(source)).toEqual([]);
  });

  it("proxies every backend prefix the application requests", () => {
    expect(uncoveredPrefixes(REQUESTED_PREFIXES, TENANT_SCOPED_ROUTES)).toEqual([]);
  });

  it("would have caught the historical /org-units omission", () => {
    // The list exactly as it stood before W0.2. This is the whole point of the
    // rewrite: the previous revision of this file compared TENANT_SCOPED_ROUTES
    // to a hand-copy of itself, so against THIS list it was still green.
    // `/users` does not appear in the expectation because nothing under src/
    // calls it yet — it is proxied ahead of the UI that will.
    const preW02Routes = [
      "/tenants",
      "/session",
      "/revenue",
      "/finance-close",
      "/exports",
      "/connectors",
      "/adsense",
      "/channels",
      "/groups",
      "/audit",
    ];
    expect(uncoveredPrefixes(REQUESTED_PREFIXES, preW02Routes)).toEqual(["/org-units"]);
  });

  // REJECT side of the matrix. One case per prefix the application actually
  // calls: drop that prefix from the list and the check must name it. This is
  // what the old hand-copied assertion could not do — it compared the list to
  // itself, so an omission was invisible on both sides.
  it.each(REQUESTED_PREFIXES)(
    "fails when %s is omitted from TENANT_SCOPED_ROUTES",
    (prefix) => {
      const withoutPrefix = TENANT_SCOPED_ROUTES.filter((route) => route !== prefix);
      expect(uncoveredPrefixes(REQUESTED_PREFIXES, withoutPrefix)).toEqual([prefix]);
    },
  );
});

describe("dev proxy route list", () => {
  it("proxies exactly the tenant-scoped routes (change-detector for additions)", () => {
    expect(TENANT_SCOPED_ROUTES).toEqual(EXPECTED_ROUTES);
  });

  it("includes /org-units so the Registry view's Company/Sector names resolve", () => {
    // Regression guard: RegistryView calls useOrgUnits() on mount. Unproxied,
    // that GET is answered by the dev server itself with an empty 404, the
    // columns show raw ids for the whole session, and the mapping form's
    // company picker holds nothing but its placeholder — with no error
    // surfaced anywhere.
    expect(TENANT_SCOPED_ROUTES).toContain("/org-units");
  });

  it("includes /users, which rides the same trusted-gateway lane", () => {
    // Listed ahead of the UI that will call it, so the first screen to need it
    // does not have to rediscover the unproxied-route 404. It is therefore
    // expected NOT to appear in REQUESTED_PREFIXES yet.
    expect(TENANT_SCOPED_ROUTES).toContain("/users");
  });

  it("builds a header-injecting entry for every route", () => {
    const proxy = buildTenantScopedProxy(TENANT_SCOPED_ROUTES, BACKEND_TARGET, GATEWAY_HEADERS);
    expect(Object.keys(proxy)).toEqual(EXPECTED_ROUTES.map(proxyContextForRoute));
    for (const route of EXPECTED_ROUTES) {
      const entry = asConfigurableEntry(proxy[proxyContextForRoute(route)], route);
      expect(entry.target).toBe(BACKEND_TARGET);
      expect(entry.changeOrigin).toBe(true);
      expect(headerMutations(entry, route)).toEqual({
        removed: TRUSTED_GATEWAY_HEADERS,
        injected: GATEWAY_HEADERS,
      });
    }
  });
});
