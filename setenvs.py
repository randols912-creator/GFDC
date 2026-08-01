import os, logging

#Because LiClipse is not working with os.getenv
def set_configs():
    if(os.path.isfile('./setenv.sh')):
            f = open("setenv.sh", "r")
            lines = f.readlines()
            for line in lines:
                if line[0] != '#':
                    command = line.split()
                    command = command[1].split('=')
                    os.environ[command[0]]=command[1][1:-1]
    logging.info('config variables set')
