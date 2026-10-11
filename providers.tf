provider "aws" {
  region = var.region

  default_tags {
    tags = {
      ManagedBy = "terraform"
      System    = "whatsnew"
      Env       = var.env
      Owner     = var.owner
    }
  }
}
