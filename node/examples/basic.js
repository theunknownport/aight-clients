import { tracedLlmCall, COST_PROCESSOR, push } from '../src/index.js'

function callLlm() {
  tracedLlmCall('gpt-4o-mini', 180, 60)
}

callLlm()
callLlm()

console.log(COST_PROCESSOR.report())

if (process.env.AIGHT_API_KEY) {
  await push(COST_PROCESSOR)
  console.log('pushed to aight.studio')
}
