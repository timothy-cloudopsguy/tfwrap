locals {
  props_candidates = [
    for ext in ["json", "yaml", "yml"] :
    "${path.module}/properties.${var.environment}.${ext}"
    if fileexists("${path.module}/properties.${var.environment}.${ext}")
  ]
  # file() on a non-existent descriptive path forces a readable plan-time error.
  props_file = length(local.props_candidates) == 1 ? local.props_candidates[0] : file(
    "ERROR: expected exactly one properties.${var.environment}.(json|yaml|yml), found ${length(local.props_candidates)}"
  )
  props = endswith(local.props_file, ".json") ? jsondecode(file(local.props_file)) : yamldecode(file(local.props_file))

  # Expose useful values
  env_name   = var.environment
  app_name   = length(trimspace(var.app_name)) > 0 ? var.app_name : "${local.props.app_name}${title(var.environment)}"
  route53    = local.props.route53
} 