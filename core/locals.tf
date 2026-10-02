locals {
  # Load the existing properties files from the cdk directory (properties.<env>.json, .yaml or .yml; JSON wins if several exist)
  props_base = "${path.module}/properties.${var.environment}"
  props_file = fileexists("${local.props_base}.json") ? "${local.props_base}.json" : (fileexists("${local.props_base}.yaml") ? "${local.props_base}.yaml" : "${local.props_base}.yml")
  props      = endswith(local.props_file, ".json") ? jsondecode(file(local.props_file)) : yamldecode(file(local.props_file))

  # Expose useful values
  env_name   = var.environment
  app_name   = length(trimspace(var.app_name)) > 0 ? var.app_name : "${local.props.app_name}${title(var.environment)}"
  route53    = local.props.route53
} 