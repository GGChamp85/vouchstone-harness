#!/usr/bin/env node
// Real smoke test for the Vouchstone MCP server -- spawns the actual
// built dist/index.js as a subprocess and talks to it over real stdio
// JSON-RPC via the official @modelcontextprotocol/sdk Client, not a
// hand-rolled protocol reimplementation. This server had never been
// built or run once before this test existed.

import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import assert from 'node:assert/strict'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const serverPath = path.join(__dirname, '..', 'dist', 'index.js')

let failures = 0
function check(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => console.log(`  ok - ${name}`))
    .catch((err) => {
      failures++
      console.error(`  FAIL - ${name}`)
      console.error(`    ${err.message}`)
    })
}

async function main() {
  console.log('Starting real MCP server subprocess:', serverPath)
  const transport = new StdioClientTransport({
    command: 'node',
    args: [serverPath],
    env: {
      ...process.env,
      // Deliberately unreachable -- proves CallTool attempts a real
      // network request rather than returning stubbed data.
      VOUCHSTONE_API_URL: 'http://127.0.0.1:1',
      VOUCHSTONE_API_KEY: 'smoke-test-key',
      VOUCHSTONE_TENANT_ID: 'smoke-test-tenant',
    },
  })

  const client = new Client({ name: 'vouchstone-smoke-test', version: '1.0.0' }, { capabilities: {} })
  await client.connect(transport)
  console.log('Connected (real stdio JSON-RPC handshake completed)\n')

  let tools
  await check('listTools() returns a real, non-empty tool list', async () => {
    const result = await client.listTools()
    tools = result.tools
    assert.ok(Array.isArray(tools) && tools.length > 0, 'expected a non-empty tools array')
  })

  await check('tool set covers KG/agents/memory/governance categories', () => {
    const names = new Set(tools.map((t) => t.name))
    for (const expected of ['kg_query', 'kg_list_sub_graphs', 'agent_list', 'memory_query_semantic', 'governance_check_policy']) {
      assert.ok(names.has(expected), `expected tool "${expected}" to be present`)
    }
    // No fabricated AI-stack tools -- the real server never had these,
    // unlike the dashboard's hardcoded "MCP Console" panel.
    for (const name of names) {
      assert.ok(!name.startsWith('ai_'), `unexpected ai_* tool "${name}" -- these were never real`)
    }
  })

  await check('every tool has a real JSON Schema inputSchema', () => {
    for (const t of tools) {
      assert.ok(t.inputSchema && t.inputSchema.type === 'object', `tool "${t.name}" missing a real inputSchema`)
    }
  })

  let resources
  await check('listResources() responds (may be empty, must not error)', async () => {
    const result = await client.listResources()
    resources = result.resources
    assert.ok(Array.isArray(resources))
  })

  await check('callTool() genuinely attempts a real network request (not a stub)', async () => {
    const result = await client.callTool({ name: 'kg_list_sub_graphs', arguments: {} })
    assert.equal(result.isError, true, 'expected a real connection failure against an unreachable API URL')
    const text = result.content?.[0]?.text || ''
    assert.ok(/error/i.test(text), `expected a real error message, got: ${text}`)
    console.log(`    (real network error surfaced: ${text.slice(0, 120)})`)
  })

  await check('callTool() with an unknown tool name returns isError, not a crash', async () => {
    const result = await client.callTool({ name: 'not_a_real_tool', arguments: {} })
    assert.equal(result.isError, true)
  })

  await client.close()

  console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : `${failures} CHECK(S) FAILED`}`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((err) => {
  console.error('Smoke test crashed:', err)
  process.exit(1)
})
