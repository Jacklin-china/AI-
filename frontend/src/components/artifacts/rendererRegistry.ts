import type { Component } from 'vue'
import CandidateArtifact from '../../domains/commerce/artifacts/CandidateArtifact.vue'
import ListingArtifact from '../../domains/commerce/artifacts/ListingArtifact.vue'
import ProductImageArtifact from '../../domains/commerce/artifacts/ProductImageArtifact.vue'
import PublishArtifact from '../../domains/commerce/artifacts/PublishArtifact.vue'
import QcArtifact from '../../domains/commerce/artifacts/QcArtifact.vue'
import RequirementArtifact from '../../domains/commerce/artifacts/RequirementArtifact.vue'
import GenericArtifactContent from './GenericArtifactContent.vue'

const renderers = new Map<string, Component>()

export function registerArtifactRenderer(schemaName: string, component: Component): void {
  renderers.set(schemaName, component)
}

export function resolveArtifactRenderer(schemaName: string): Component {
  return renderers.get(schemaName) ?? GenericArtifactContent
}

registerArtifactRenderer('commerce.requirement', RequirementArtifact)
registerArtifactRenderer('commerce.candidate_list', CandidateArtifact)
registerArtifactRenderer('commerce.candidate_analysis', CandidateArtifact)
registerArtifactRenderer('commerce.sku_selection', CandidateArtifact)
registerArtifactRenderer('commerce.pricing_result', CandidateArtifact)
registerArtifactRenderer('commerce.listing_draft', ListingArtifact)
registerArtifactRenderer('commerce.localized_listing', ListingArtifact)
registerArtifactRenderer('commerce.product_image', ProductImageArtifact)
registerArtifactRenderer('commerce.qc_report', QcArtifact)
registerArtifactRenderer('commerce.marketplace_draft', PublishArtifact)
registerArtifactRenderer('commerce.publish_result', PublishArtifact)
