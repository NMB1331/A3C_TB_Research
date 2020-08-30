## NOTE: This needs to be updated to actually run A3C TB. Right now, this is just A3C.

#!/bin/bash
#PBS -V       					##load env parameters
#PBS -N A3C_TB         				## Job name
#PBS -M nathaniel.burley@wsu.edu      		## Email of notification, CHANGE TO YOURS
#PBS -m abe                    			## Email if abort, begins, ends
#PBS -l walltime=80:00:00                       ## Run time (day:hh:mm:ss), usually 3+ days (currently 2 hrs. for a test)
#PBS -l mem=32gb                           	## Memory requirements, 16G should be good, 32G to be safe
#PBS -l nodes=1:ppn=8                    	## Number of processor needed per task, 8 is a good balance between speed vs.#jobs can run
#PBS -q datascience                             ## queue on "datascience" group, can also use "batch" (I'm not sure what's the diff tho..)
#PBS -k o					## keep output file
#PBS -e /data/datascience/A3C_TB_Research/logs/TB       ## error file directory; this is where you can see the output logs
#PBS -o /data/datascience/A3C_TB_Research/logs/TB       ## output file directory
#PBS -t 1-3 					## number of trials to run; here submit 3 trials together, usually want 5+ trials to get the average

module load python/3.7
module load go/1.11.5
module load singularity/3.4.2/go/1.11.5

echo "Starting singularity on host ${PBS_O_HOST}"

image=/data/datascience/A3C_TB_Research/production_cpu.simg
cwd=/data/datascience/A3C_TB_Research

cd /data/datascience/A3C_TB_Research

game=MsPacman # Pong

singularity exec -H $cwd $image python3 DeepRL/run_experiment.py \
    --gym-env=${game}NoFrameskip-v4 \
    --parallel-size=16 \
    --max-time-step-fraction=0.5 \
    --use-mnih-2015 --padding=SAME --input-shape=88 \
    --append-experiment-num=${PBS_ARRAYID} \
    --transformed-bellman \ ## Use the Transformed Bellman operator in the experiment
    --save-to=/data/datascience/A3C_TB_Research/results/ \
