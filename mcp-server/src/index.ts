#!/usr/bin/env node

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ListResourcesRequestSchema,
  ReadResourceRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
import { KnowledgeGraphTools } from './tools/knowledge-graph.js'
import { AgentTools } from './tools/agents.js'
import { MemoryTools } from './tools/memory.js'
import { KnowledgeGraphResources } from './resources/knowledge-graph.js'

const API_BASE = process.env.VOUCHSTONE_API_URL || 'http://localhost:8000'
const API_KEY = process.env.VOUCHSTONE_API_KEY || ''
const TENANT_ID = process.env.VOUCHSTONE_TENANT_ID || ''

async function apiCall(path: string, options: RequestInit = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${API_KEY}`,
      'X-Tenant-ID': TENANT_ID,
      ...options.headers,
    },
  })
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`)
  return res.json()
}

const kgTools = new KnowledgeGraphTools(apiCall)
const agentTools = new AgentTools(apiCall)
const memoryTools = new MemoryTools(apiCall)
const kgResources = new KnowledgeGraphResources(apiCall, TENANT_ID)

const server = new Server(
  { name: 'vouchstone', version: '1.0.0' },
  { capabilities: { tools: {}, resources: {} } },
)

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    ...kgTools.definitions(),
    ...agentTools.definitions(),
    ...memoryTools.definitions(),
  ],
}))

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params
  const allHandlers = { ...kgTools.handlers(), ...agentTools.handlers(), ...memoryTools.handlers() }
  const handler = allHandlers[name]
  if (!handler) return { content: [{ type: 'text', text: `Unknown tool: ${name}` }], isError: true }
  try {
    const result = await handler(args || {})
    return { content: [{ type: 'text', text: typeof result === 'string' ? result : JSON.stringify(result, null, 2) }] }
  } catch (err: any) {
    return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true }
  }
})

server.setRequestHandler(ListResourcesRequestSchema, async () => ({
  resources: kgResources.definitions(),
}))

server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
  const result = await kgResources.read(request.params.uri)
  return { contents: [{ uri: request.params.uri, mimeType: 'application/json', text: JSON.stringify(result, null, 2) }] }
})

async function main() {
  const transport = new StdioServerTransport()
  await server.connect(transport)
}

main().catch(console.error)
