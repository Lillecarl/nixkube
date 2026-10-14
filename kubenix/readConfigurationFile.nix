/**
  Read exact preview text when supplied; otherwise read the configuration file.
*/
preview: configuration:
if preview && configuration.text != null then
  configuration.text
else
  builtins.readFile configuration.file
