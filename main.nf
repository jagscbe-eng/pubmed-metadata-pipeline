nextflow.enable.dsl=2

/*
========================================================================================
    A Minimalist Nextflow Workflow for Biopharma Metadata Extraction
========================================================================================
*/

// Define the input channel: Look for all text files inside your OneDrive input folder
params.input_dir   = "C:/Users/jagsc/OneDrive/abstracts_input/*.txt"
params.script_path = "C:/Users/jagsc/OneDrive/pubmed_metadata_extractor.py"
params.output_dir  = "C:/Users/jagsc/OneDrive/metadata_output"

process EXTRACT_METADATA {
    tag "Processing abstracts"
    publishDir params.output_dir, mode: 'copy'

    // In a production environment, you would specify a Docker or Singularity container here
    // container 'biocontainers/python-anthropic:latest'

    input:
    path input_files
    path script

    output:
    path "*.json", emit: metadata_files
    stdout emit: pipeline_log

    script:
    // input_files/script are staged into this task's own work directory, so "."
    // points at the staged copies rather than the original OneDrive paths.
    """
    python ${script} --input_dir . --output_dir .
    """
}

workflow {
    // 1. Create a channel from your input files
    abstracts_ch = Channel.fromPath(params.input_dir).collect()
    script_ch    = Channel.fromPath(params.script_path)

    // 2. Pass the channels into your process
    EXTRACT_METADATA(abstracts_ch, script_ch)

    // 3. Print the workflow execution logs directly to the console
    EXTRACT_METADATA.out.pipeline_log.view()
}
